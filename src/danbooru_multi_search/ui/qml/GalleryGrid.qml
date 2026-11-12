import QtQuick
import QtQuick.Controls

GridView {
    id: grid
    objectName: "galleryGrid"
    required property var favoriteSelection
    required property var cacheController
    property QtObject theme: AppTheme {}
    signal postActivated(int row)
    clip: true
    cellWidth: Math.max(225, width / Math.max(1, Math.floor(width / 245)))
    cellHeight: 340
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    delegate: PostCard {
        readonly property var favoriteState: {
            const revision = grid.favoriteSelection.revision
            return grid.favoriteSelection.postState(postKey)
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
