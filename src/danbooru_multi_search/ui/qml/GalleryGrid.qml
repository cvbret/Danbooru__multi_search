import QtQuick
import QtQuick.Controls

GridView {
    id: grid
    objectName: "galleryGrid"
    required property var favoriteSelection
    required property var cacheController
    required property var downloads
    property QtObject theme: AppTheme {}
    signal postActivated(int row)
    signal downloadPrepared(string token)
    signal reachedEnd()
    property var anchorBeforeSort: null
    function topVisibleItem(top) {
        let first = null
        for (const row of contentItem.children) {
            if (typeof row.postKey !== "string" || row.y + row.height <= top || row.y >= top + height)
                continue
            if (!first || row.y < first.y || (row.y === first.y && row.x < first.x)) first = row
        }
        return first
    }
    property int scrollOperation: -1
    function continueAtEnd() {
        const own = scrollOperation === model.displayOperationId
        scrollOperation = -1
        if (own && visible && count > 0 && contentHeight > height && atYEnd)
            reachedEnd()
    }
    onMovementStarted: scrollOperation = model.displayOperationId
    onMovementEnded: continueAtEnd()
    clip: true
    cellWidth: Math.max(225, width / Math.max(1, Math.floor(width / 245)))
    cellHeight: 340
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {
        onPressedChanged: {
            if (pressed) grid.scrollOperation = grid.model.displayOperationId
            else grid.continueAtEnd()
        }
    }
    Connections {
        target: grid.model
        function onViewReordering() {
            grid.anchorBeforeSort = null
            if (!grid.visible || grid.contentY <= grid.originY || !grid.count) return
            const row = grid.topVisibleItem(grid.contentY)
            if (row) grid.anchorBeforeSort = {key: row.postKey, offset: grid.contentY - row.y, operation: grid.model.displayOperationId}
        }
        function onModelReset() {
            const anchor = grid.anchorBeforeSort
            grid.anchorBeforeSort = null
            if (!anchor || anchor.operation !== grid.model.displayOperationId) return
            const revision = grid.model.revision
            Qt.callLater(function() {
                if (!grid.visible || grid.model.revision !== revision) return
                const index = grid.model.rowForPostKey(anchor.key)
                if (index < 0) return
                grid.positionViewAtIndex(index, GridView.Beginning)
                grid.forceLayout()
                const row = grid.itemAtIndex(index)
                if (row) grid.contentY = Math.max(grid.originY, Math.min(row.y + anchor.offset,
                    grid.originY + Math.max(0, grid.contentHeight - grid.height)))
            })
        }
    }

    delegate: PostCard {
        readonly property var favoriteState: {
            const revision = grid.favoriteSelection.revision
            return grid.favoriteSelection.postState(postKey)
        }
        readonly property var downloadState: grid.downloads.galleryDownloadState(postKey, actionRevision)
        downloadActionVisible: true
        downloadEnabled: downloadState.enabled
        downloadReason: downloadState.reason
        onDownloadRequested: function(key, revision) {
            const token = grid.downloads.captureGalleryPost(key, revision)
            if (token.length) grid.downloadPrepared(token)
        }
        postKey: grid.favoriteSelection.postKeyAt(index, grid.favoriteSelection.modelRevision)
        selectionKey: postKey
        actionRevision: grid.favoriteSelection.modelRevision
        selected: favoriteState.selected
        favoriteKnown: favoriteState.known
        favoriteSaved: favoriteState.saved
        favoriteEnabled: favoriteState.enabled
        cacheController: grid.cacheController
        theme: grid.theme
        onSelectionRequested: function(key, desired, revision) {
            grid.favoriteSelection.setSelected(key, desired, revision)
        }
        onFavoriteRequested: function(key, desired, revision) {
            grid.favoriteSelection.setPostFavorite(key, desired, revision)
        }
        onDetailRequested: function(key, revision, row) {
            if (revision === grid.favoriteSelection.modelRevision
                    && key === grid.favoriteSelection.postKeyAt(row, revision))
                grid.postActivated(row)
        }
    }
}
