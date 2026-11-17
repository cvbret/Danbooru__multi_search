import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: root
    property QtObject theme: AppTheme {}
    visible: true
    x: sessionController.initialWindowX
    y: sessionController.initialWindowY
    width: sessionController.initialWindowWidth
    height: sessionController.initialWindowHeight
    minimumWidth: sessionController.minimumWindowWidth
    minimumHeight: sessionController.minimumWindowHeight
    title: qsTr("Danbooru Multi Search")
    color: theme.background
    font.pixelSize: theme.fontSize

    palette.window: theme.background
    palette.windowText: theme.text
    palette.base: theme.card
    palette.alternateBase: theme.raised
    palette.text: theme.text
    palette.button: theme.surface
    palette.buttonText: theme.text
    palette.highlight: theme.accent
    palette.highlightedText: theme.accentForeground
    palette.toolTipBase: theme.surface
    palette.toolTipText: theme.text
    palette.dark: theme.accent
    palette.brightText: theme.accentForeground
    palette.mid: theme.borderStrong
    palette.light: theme.raised
    palette.placeholderText: theme.subtleText
    palette.disabled.button: theme.panel
    palette.disabled.buttonText: theme.subtleText
    palette.disabled.text: theme.subtleText

    ApplicationBackground {
        anchors.fill: parent
        controller: appearanceController
        theme: root.theme
    }

    AppearancePanel {
        id: appearancePanel
        controller: appearanceController
        theme: root.theme
    }
    EaglePanel { id: eaglePanel; controller: eagleController; theme: root.theme }

    FavoriteTransferDialog {
        id: favoriteTransferDialog
        controller: favoriteTransfer
        theme: root.theme
    }

    DownloadRequestDialog {
        id: downloadRequestDialog
        controller: downloadUi
        theme: root.theme
    }

    CacheCleanupDialog {
        id: cacheCleanupDialog
        controller: cacheCleanup
        theme: root.theme
    }

    DownloadRequestDialog {
        id: poolDownloadRequestDialog
        objectName: "poolDownloadRequestDialog"
        controller: poolDownloads
        theme: root.theme
        title: qsTr("下载整个系列作品池")
        summaryText: qsTr("按原始页序准备 %1 个页面。部分作品可能缺失或没有原文件。").arg(controller.pending.count)
        explanationText: qsTr("确认后分批读取元数据并加入下载。文件名包含五位原页码与作品编号，缺失页面保留空缺页号。取消窗口不会创建计划。")
    }
    PoolDownloadPanel {
        id: poolDownloadPanel; controller: poolDownloads; downloads: downloadUi; theme: root.theme
        onBatchRequested: function(key) { root.showPage("downloads"); downloadUi.openPersistedBatch(key) }
    }
    Connections {
        target: poolDownloads
        function onQueued(key) { root.showPage("downloads"); poolDownloadPanel.showRecords() }
    }

    function requestCleanup(kind) {
        if (cacheCleanup.prepareCleanup(kind) || cacheCleanup.noticeText.length > 0)
            cacheCleanupDialog.open()
    }

    property bool artistSuggestionsSuppressed: false
    property bool savedQuerySuggestionsSuppressed: false
    signal queryEditorRequested()

    function openSavedQueries() {
        savedQuerySuggestionsSuppressed = true
        tagSuggestionController.closeSuggestions()
        savedQueryPanel.open()
    }

    SavedQueryPanel {
        id: savedQueryPanel
        controller: savedQueries
        theme: root.theme
        onDismissed: Qt.callLater(function() { root.savedQuerySuggestionsSuppressed = false })
    }

    Connections {
        target: savedQueries
        function onDraftLoaded() {
            root.showPage("browse")
            root.queryEditorRequested()
            if (root.compactLayout)
                queryDrawer.open()
            savedQueryPanel.close()
        }
    }

    function openArtistCombinations(parentPath) {
        artistSuggestionsSuppressed = true
        tagSuggestionController.closeSuggestions()
        artistCombinationPanel.insertionPath = parentPath
        artistCombinationPanel.open()
    }

    ArtistCombinationPanel {
        id: artistCombinationPanel
        controller: artistCombinations
        theme: root.theme
        onDismissed: Qt.callLater(function() { root.artistSuggestionsSuppressed = false })
    }

    Connections {
        target: artistCombinations
        function onDraftApplied() {
            root.showPage("browse")
            root.queryEditorRequested()
            if (root.compactLayout)
                queryDrawer.open()
            artistCombinationPanel.close()
        }
    }

    property string activePage: "browse"

    function showPage(pageName) {
        if (detailController.isOpen)
            return
        if (pageName !== "pool")
            poolNavigation.invalidate()
        queryDrawer.close()
        activePage = pageName
        if (pageName === "favorites")
            favoriteCollection.setActive(true)
        downloadUi.setActive(pageName === "downloads")
    }

    RowLayout {
        id: pageNavigation
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 12
        height: 40
        spacing: 8
        ActionButton {
            objectName: "browseNavigationButton"
            text: qsTr("浏览")
            checkable: true
            autoExclusive: true
            checked: root.activePage === "browse"
            enabled: !detailController.isOpen
            onClicked: root.showPage("browse")
        }
        ActionButton {
            objectName: "favoritesNavigationButton"
            text: qsTr("收藏")
            checkable: true
            autoExclusive: true
            checked: root.activePage === "favorites"
            enabled: !detailController.isOpen
            onClicked: root.showPage("favorites")
        }
        ActionButton {
            objectName: "downloadsNavigationButton"
            text: qsTr("下载")
            checkable: true
            autoExclusive: true
            checked: root.activePage === "downloads"
            enabled: !detailController.isOpen
            onClicked: root.showPage("downloads")
        }
        Item { Layout.fillWidth: true }
    }

    property bool compactLayout: width < 980
    property real queryPanelWidth: 350
    property bool stateTrackingReady: false
    property bool windowStateReportPending: false
    readonly property var browserSession: sessionController

    function reportWindowState() {
        if (!stateTrackingReady
                || (visibility !== Window.Windowed
                    && visibility !== Window.Maximized))
            return
        sessionController.updateWindowState(
            x, y, width, height,
            visibility === Window.Maximized,
            visibility === Window.Minimized
        )
    }

    function scheduleWindowStateReport() {
        if (!stateTrackingReady || windowStateReportPending)
            return
        windowStateReportPending = true
        Qt.callLater(function() {
            windowStateReportPending = false
            reportWindowState()
        })
    }

    function reportGalleryPosition() {
        if (!stateTrackingReady
                || detailController.isOpen
                || pageStack.currentIndex !== 0
                || !galleryPage.visible
                || searchController.postsModel.count === 0
                || sessionController.activeHistoryId < 1)
            return
        const maximum = Math.max(
            galleryGrid.originY,
            galleryGrid.originY + galleryGrid.contentHeight - galleryGrid.height
        )
        const boundedContentY = Math.max(
            galleryGrid.originY,
            Math.min(galleryGrid.contentY, maximum)
        )
        let index = -1
        let firstY = Number.POSITIVE_INFINITY
        let firstX = Number.POSITIVE_INFINITY
        for (let candidate = 0;
             candidate < searchController.postsModel.count;
             ++candidate) {
            const candidateItem = galleryGrid.itemAtIndex(candidate)
            if (!candidateItem
                    || candidateItem.y + candidateItem.height <= boundedContentY
                    || candidateItem.y >= boundedContentY + galleryGrid.height)
                continue
            if (candidateItem.y < firstY
                    || (candidateItem.y === firstY && candidateItem.x < firstX)) {
                index = candidate
                firstY = candidateItem.y
                firstX = candidateItem.x
            }
        }
        if (index < 0)
            return
        const item = galleryGrid.itemAtIndex(index)
        const postId = searchController.postsModel.postIdAt(index)
        if (!item || postId < 1)
            return
        sessionController.updateM2GalleryPosition(
            sessionController.activeGalleryOperationId,
            sessionController.activeGalleryExecutionId,
            sessionController.activeGalleryViewGeneration,
            postId,
            index,
            item.y - boundedContentY,
            boundedContentY,
            searchController.postsModel.revision
        )
    }

    function restoreHistoryPosition() {
        galleryGrid.forceLayout()
        const plan = sessionController.takeGalleryRestorePlan(
            galleryGrid.originY,
            galleryGrid.contentHeight,
            galleryGrid.height
        )
        if (!plan.available)
            return
        if (!sessionController.galleryRestorePlanCurrent(
                plan.token, plan.modelRevision)) {
            sessionController.finishGalleryRestorePlan(plan.token)
            return
        }
        if (plan.index < 0 || !plan.hasAnchor) {
            galleryGrid.currentIndex = plan.index
            galleryGrid.contentY = plan.absoluteContentY
            sessionController.finishGalleryRestorePlan(plan.token)
            return
        }
        Qt.callLater(function() {
            if (!sessionController.galleryRestorePlanCurrent(
                    plan.token, plan.modelRevision)) {
                sessionController.finishGalleryRestorePlan(plan.token)
                return
            }
            galleryGrid.currentIndex = plan.index
            galleryGrid.positionViewAtIndex(plan.index, GridView.Beginning)
            galleryGrid.forceLayout()
            const item = galleryGrid.itemAtIndex(plan.index)
            if (!item) {
                galleryGrid.contentY = plan.absoluteContentY
                sessionController.finishGalleryRestorePlan(plan.token)
                return
            }
            const maximum = Math.max(
                galleryGrid.originY,
                galleryGrid.originY + galleryGrid.contentHeight - galleryGrid.height
            )
            galleryGrid.contentY = Math.max(
                galleryGrid.originY,
                Math.min(item.y - plan.anchorOffset, maximum)
            )
            sessionController.finishGalleryRestorePlan(plan.token)
        })
    }

    function openPostDetails(row) {
        queryDrawer.close()
        galleryGrid.currentIndex = row
        tagSuggestionController.closeSuggestions()
        detailNavigation.openBrowsePost(
            row,
            galleryGrid.contentY,
            galleryGrid.currentIndex,
            galleryGrid.width,
            galleryGrid.height,
            root.width,
            root.height
        )
    }

    function returnToGallery() {
        if (detailNavigation.sourcePage === "pool") {
            poolNavigation.returnToPool()
            return
        }
        if (!detailNavigation.closeDetail())
            return
        if (detailNavigation.sourcePage === "favorites") {
            root.activePage = "favorites"
            favoritesPage.scheduleRestorePosition()
        } else {
            root.activePage = "browse"
            Qt.callLater(root.restoreGallerySnapshot)
        }
    }

    function restoreGallerySnapshot() {
        galleryGrid.forceLayout()
        const plan = detailNavigation.takeGalleryRestorePlan(
            searchController.postsModel,
            galleryGrid.width,
            galleryGrid.height,
            root.width,
            root.height,
            galleryGrid.originY,
            galleryGrid.contentHeight
        )
        if (!plan.available)
            return
        galleryGrid.currentIndex = plan.index
        galleryGrid.contentY = plan.contentY
    }

    function startQueryFromDetailTag(tagName) {
        root.activePage = "browse"
        if (!searchController.applyAdvancedText(tagName))
            return
        searchController.executeStandard()
    }

    function addDetailTagToDraft(tagName) {
        if (detailNavigation.sourcePage === "favorites" || detailNavigation.sourcePage === "pool") {
            poolNavigation.invalidate()
            detailNavigation.closeDetail()
            root.activePage = "browse"
            if (root.compactLayout)
                queryDrawer.open()
        }
        searchController.addVisualTag(null, tagName)
    }

    Component.onCompleted: {
        if (sessionController.startMaximized) {
            showMaximized()
            Qt.callLater(function() {
                stateTrackingReady = true
                scheduleWindowStateReport()
            })
        } else {
            stateTrackingReady = true
            scheduleWindowStateReport()
        }
    }

    onXChanged: scheduleWindowStateReport()
    onYChanged: scheduleWindowStateReport()
    onWidthChanged: scheduleWindowStateReport()
    onHeightChanged: scheduleWindowStateReport()
    onVisibilityChanged: scheduleWindowStateReport()
    onCompactLayoutChanged: {
        if (!compactLayout && queryDrawer.opened)
            queryDrawer.close()
    }
    onClosing: {
        windowStateReportPending = false
        reportWindowState()
    }

    FileDialog {
        id: tagCsvDialog
        objectName: "tagCsvDialog"
        title: qsTr("选择本地标签 CSV")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("CSV 文件 (*.csv)"), qsTr("所有文件 (*)")]
        onAccepted: tagSuggestionController.importCsv(selectedFile)
    }

    Popup {
        id: imageCachePopup
        objectName: "imageCachePopup"
        parent: Overlay.overlay
        width: Math.min(460, root.width - 48)
        modal: true
        focus: true
        anchors.centerIn: parent
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        onAboutToShow: {
            cacheLimitInput.value = Math.round(
                imageCacheController.maximumBytes / 1048576
            )
            imageCacheController.refreshUsage()
        }

        background: Rectangle {
            color: theme.surface
            border.color: theme.borderStrong
            radius: theme.radius
        }

        contentItem: ColumnLayout {
            spacing: theme.gap
            Label {
                text: qsTr("图片缓存")
                color: theme.text
                font.pixelSize: 20
                font.bold: true
            }
            Label {
                objectName: "imageCacheUsageLabel"
                text: qsTr("当前占用：%1 MiB")
                      .arg((imageCacheController.currentBytes / 1048576).toFixed(2))
                color: theme.secondaryText
            }
            RowLayout {
                Layout.fillWidth: true
                Label { text: qsTr("上限（MiB）"); color: theme.secondaryText }
                SpinBox {
                    id: cacheLimitInput
                    objectName: "imageCacheLimitInput"
                    from: 64
                    to: 16384
                    value: 512
                    editable: true
                    Layout.fillWidth: true
                }
                ActionButton {
                    objectName: "applyImageCacheLimitButton"
                    text: qsTr("保存上限")
                    onClicked: imageCacheController.setMaximumBytes(
                        cacheLimitInput.value * 1048576
                    )
                }
            }
            Label {
                Layout.fillWidth: true
                visible: text.length > 0
                text: imageCacheController.statusText
                color: theme.warning
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                ActionButton {
                    objectName: "clearImageCacheButton"
                    text: qsTr("清理图片缓存")
                    onClicked: root.requestCleanup("images")
                }
                Item { Layout.fillWidth: true }
                ActionButton { text: qsTr("关闭"); onClicked: imageCachePopup.close() }
            }
            ActionButton {
                objectName: "clearCheckpointCacheButton"
                Layout.fillWidth: true
                text: qsTr("清理执行检查点 / 扫描结果")
                enabled: cacheCleanup.canWrite
                onClicked: root.requestCleanup("checkpoints")
            }
            Label { Layout.fillWidth: true; text: cacheCleanup.noticeText; visible: text.length > 0; color: theme.warning; wrapMode: Text.Wrap }
        }
    }

    Component {
        id: queryWorkspaceComponent

        Rectangle {
            id: workspace
            color: theme.panel
            border.color: theme.border
            radius: theme.radius

            Connections {
                target: root
                function onQueryEditorRequested() { workspaceTabs.currentIndex = 0 }
            }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 20
                spacing: 16

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        Layout.fillWidth: true
                        text: qsTr("搜索")
                        color: theme.text
                        font.pixelSize: 19
                        font.bold: true
                    }
                    ActionButton {
                        objectName: "importTagsButton"
                        text: tagSuggestionController.importBusy
                              ? qsTr("导入中") : qsTr("标签库")
                        enabled: !tagSuggestionController.importBusy
                        onClicked: tagCsvDialog.open()
                    }
                }

                Label {
                    objectName: "tagImportStatus"
                    Layout.fillWidth: true
                    text: tagSuggestionController.importStatusText
                    color: tagSuggestionController.importBusy ? theme.accentText : theme.subtleText
                    elide: Text.ElideRight
                    font.pixelSize: 11
                }

                TabBar {
                    id: workspaceTabs
                    objectName: "workspaceTabs"
                    Layout.fillWidth: true
                    palette.dark: theme.surface
                    palette.brightText: theme.mutedText
                    palette.window: theme.selected
                    palette.windowText: theme.text
                    TabButton { text: qsTr("查询") }
                    TabButton { text: qsTr("筛选") }
                    TabButton {
                        objectName: "historyButton"
                        text: qsTr("历史")
                    }
                }

                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: workspaceTabs.currentIndex

                    QueryEditorPanel {
                        theme: root.theme
                        controller: searchController
                        tagController: tagSuggestionController
                        savedQueryController: savedQueries
                        externalSuggestionSuppressed: root.artistSuggestionsSuppressed || root.savedQuerySuggestionsSuppressed || queryCheckpoints.draftLoading
                        onSavedQueriesRequested: root.openSavedQueries()
                        onArtistCombinationsRequested: function(parentPath) { root.openArtistCombinations(parentPath) }
                        onCloseRequested: {
                            if (root.compactLayout)
                                queryDrawer.close()
                        }
                    }
                    QueryFilterPanel {
                        theme: root.theme
                        controller: searchController
                    }
                    HistoryPanel {
                        theme: root.theme
                        sessionController: root.browserSession
                        checkpointController: queryCheckpoints
                        cleanupEnabled: cacheCleanup.canWrite
                        onCleanupRequested: function(kind) { root.requestCleanup(kind) }
                        onSavedQueriesRequested: root.openSavedQueries()
                    }
                }
            }
        }
    }

    Drawer {
        id: queryDrawer
        objectName: "queryDrawer"
        width: Math.min(430, root.width * 0.9)
        height: root.height
        edge: Qt.LeftEdge
        modal: true
        interactive: root.compactLayout
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onClosed: tagSuggestionController.closeSuggestions()

        Loader {
            anchors.fill: parent
            anchors.margins: 8
            active: root.compactLayout
            sourceComponent: queryWorkspaceComponent
        }
    }

    Item {
        id: wideQueryPanel
        visible: root.activePage === "browse" && !root.compactLayout && !detailController.isOpen
        width: visible ? root.queryPanelWidth : 0
        anchors.left: parent.left
        anchors.top: pageNavigation.bottom
        anchors.bottom: parent.bottom

        Loader {
            anchors.fill: parent
            anchors.margins: 10
            active: !root.compactLayout
            sourceComponent: queryWorkspaceComponent
        }

        Rectangle {
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: 5
            color: resizeHandle.containsMouse || resizeHandle.pressed
                   ? theme.accent : "transparent"
            MouseArea {
                id: resizeHandle
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.SizeHorCursor
                property real pressX: 0
                property real startWidth: 0
                onPressed: function(mouse) {
                    pressX = mouse.x
                    startWidth = root.queryPanelWidth
                }
                onPositionChanged: function(mouse) {
                    if (!pressed)
                        return
                    root.queryPanelWidth = Math.max(
                        320,
                        Math.min(520, startWidth + mouse.x - pressX)
                    )
                }
            }
        }
    }

    StackLayout {
        id: pageStack
        anchors.left: wideQueryPanel.right
        anchors.right: parent.right
        anchors.top: pageNavigation.bottom
        anchors.bottom: parent.bottom
        anchors.margins: 20
        currentIndex: detailController.isOpen ? 3 : (root.activePage === "favorites" ? 1 : root.activePage === "downloads" ? 2 : root.activePage === "pool" ? 4 : root.activePage === "reader" ? 5 : 0)

        Item {
            id: galleryPage

            ColumnLayout {
                anchors.fill: parent
                spacing: theme.gap

                RowLayout {
                    id: browseHeader
                    objectName: "browseHeader"
                    Layout.fillWidth: true
                    spacing: 12
                    ActionButton {
                        objectName: "openQueryDrawerButton"
                        visible: root.compactLayout
                        text: qsTr("查询条件")
                        onClicked: queryDrawer.open()
                    }
                    ColumnLayout {
                        Layout.maximumWidth: Math.max(
                            120, browseHeader.width - browseTools.implicitWidth
                                 - (root.compactLayout ? 120 : 0) - 32)
                        Layout.minimumWidth: 0
                        spacing: 4
                        Label {
                            text: qsTr("Danbooru")
                            color: theme.text
                            font.pixelSize: 25
                            font.bold: true
                        }
                        Label {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            text: searchController.hasDisplayedGallery
                                  ? (searchController.galleryExpression.length > 0
                                     ? searchController.galleryExpression
                                     : searchController.galleryMode === "rank"
                                       ? qsTr("近期热门") : qsTr("最新作品"))
                                  : qsTr("作品浏览 · 输入标签，发现喜欢的作品")
                            color: theme.mutedText
                            elide: Text.ElideRight
                        }
                    }
                    Item { Layout.fillWidth: true; Layout.minimumWidth: 0 }
                    RowLayout {
                        id: browseTools
                        objectName: "browseTools"
                        spacing: 8
                        ActionButton { objectName: "eagleButton"; text: qsTr("Eagle"); onClicked: eaglePanel.open() }
                        ActionButton {
                            objectName: "imageCacheButton"
                            text: qsTr("缓存")
                            onClicked: imageCachePopup.open()
                        }
                        ActionButton {
                            objectName: "appearanceButton"
                            text: qsTr("外观")
                            onClicked: appearancePanel.open()
                        }
                    }
                }

                QueryStatusBar {
                    id: queryStatus
                    objectName: "queryStatusBar"
                    theme: root.theme
                    Layout.fillWidth: true
                    controller: searchController
                    sessionController: root.browserSession
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: theme.smallGap
                    Label {
                        height: theme.controlHeight
                        verticalAlignment: Text.AlignVCenter
                        text: qsTr("已选择 %1 件作品").arg(gallerySelection.selectedCount)
                        color: theme.secondaryText
                    }
                    ActionButton {
                        objectName: "saveSelectedFavoritesButton"
                        text: qsTr("本地收藏已选作品")
                        enabled: gallerySelection.canSaveSelected
                        property var pressedAction: null
                        onPressed: pressedAction = {
                            "selection": gallerySelection.revision,
                            "model": gallerySelection.modelRevision
                        }
                        onClicked: {
                            if (pressedAction)
                                gallerySelection.saveSelected(pressedAction.selection,
                                    pressedAction.model)
                            pressedAction = null
                        }
                    }
                    ActionButton {
                        objectName: "clearFavoriteSelectionButton"
                        text: qsTr("清除选择")
                        enabled: gallerySelection.selectedCount > 0
                        onClicked: gallerySelection.clearSelection()
                    }
                    ActionButton {
                        objectName: "downloadSelectedPostsButton"
                        text: qsTr("下载已选原文件")
                        enabled: gallerySelection.selectedCount > 0
                        property var pressedAction: null
                        onPressed: pressedAction = {"selection": gallerySelection.revision, "model": gallerySelection.modelRevision}
                        onClicked: {
                            if (pressedAction) {
                                const token = downloadUi.captureGallerySelection(pressedAction.selection, pressedAction.model)
                                if (token.length) downloadRequestDialog.showRequest(token)
                            }
                            pressedAction = null
                        }
                    }
                }
                Label { Layout.fillWidth: true; text: downloadUi.errorText || downloadUi.noticeText; visible: text.length > 0; color: downloadUi.errorText.length ? theme.error : theme.secondaryText; wrapMode: Text.Wrap }
                RowLayout {
                    Layout.fillWidth: true
                    visible: favoriteController.noticeText.length > 0
                    Label {
                        objectName: "favoriteNotice"
                        Layout.fillWidth: true
                        text: favoriteController.noticeText
                        color: theme.error
                        wrapMode: Text.Wrap
                    }
                    ActionButton {
                        objectName: "retryFavoriteStatesButton"
                        text: qsTr("重试收藏状态")
                        enabled: !favoriteController.busy
                        onClicked: favoriteController.retryStates()
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: 220
                    radius: theme.radius
                    color: theme.canvas
                    border.color: theme.border
                    clip: true

                    GalleryGrid {
                        id: galleryGrid
                        objectName: "galleryGrid"
                        anchors.fill: parent
                        anchors.margins: 10
                        model: searchController.postsModel
                        favoriteSelection: gallerySelection
                        cacheController: imageCacheController
                        downloads: downloadUi
                        onDownloadPrepared: function(token) { downloadRequestDialog.showRequest(token) }
                        theme: root.theme
                        onPostActivated: function(row) { root.openPostDetails(row) }
                        onContentYChanged: root.reportGalleryPosition()
                        onCurrentIndexChanged: root.reportGalleryPosition()
                        onWidthChanged: Qt.callLater(root.reportGalleryPosition)
                        onHeightChanged: Qt.callLater(root.reportGalleryPosition)
                        onContentHeightChanged: Qt.callLater(root.reportGalleryPosition)

                    }

                    ColumnLayout {
                        anchors.centerIn: parent
                        width: Math.min(540, parent.width - 48)
                        visible: searchController.postsModel.count === 0
                        spacing: 14
                        Label {
                            objectName: "galleryEmptyState"
                            Layout.fillWidth: true
                            text: searchController.queryState === "idle"
                                  ? qsTr("从一个标签开始探索")
                                  : queryStatus.emptyStateText
                            color: searchController.queryState === "failed"
                                   ? theme.error : theme.text
                            font.pixelSize: 23
                            font.bold: true
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: searchController.queryState === "idle"
                            text: root.compactLayout
                                  ? qsTr("打开“查询条件”输入英文标签，或查看近期热门与最新作品。")
                                  : qsTr("在左侧输入英文标签并点击“搜索作品”，也可以查看近期热门与最新作品。")
                            color: theme.mutedText
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.Wrap
                            lineHeight: 1.4
                        }
                        ActionButton {
                            Layout.alignment: Qt.AlignHCenter
                            visible: root.compactLayout
                                     && searchController.queryState === "idle"
                            text: qsTr("打开查询条件")
                            onClicked: queryDrawer.open()
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: theme.smallGap
                    Label {
                        objectName: "rangeDescription"
                        Layout.fillWidth: true
                        text: searchController.rangeDescription.length > 0
                              ? searchController.rangeDescription
                              : qsTr("搜索仅在你执行查询时开始")
                        color: theme.mutedText
                        wrapMode: Text.Wrap
                    }
                    ActionButton {
                        objectName: "refreshButton"
                        text: qsTr("刷新")
                        enabled: searchController.canRefresh
                        onClicked: searchController.refresh()
                    }
                    ActionButton {
                        objectName: "retryButton"
                        text: qsTr("重试失败请求")
                        visible: searchController.canRetry
                        enabled: searchController.canRetry
                        onClicked: searchController.retryFailedPage()
                    }
                }
            }
        }

        FavoritesPage {
            id: favoritesPage
            controller: favoriteCollection
            onTransferRequested: favoriteTransferDialog.open()
            onArtistCombinationsRequested: root.openArtistCombinations(null)
            cacheController: imageCacheController
            theme: root.theme
        }

        DownloadsPage { controller: downloadUi; theme: root.theme; onPoolDownloadsRequested: poolDownloadPanel.showRecords() }

        DetailPage {
            backLabel: detailNavigation.sourcePage === "pool" ? qsTr("返回作品池") : detailNavigation.sourcePage === "favorites" ? qsTr("返回收藏") : qsTr("返回画廊")
            theme: root.theme
            controller: detailController
            favorites: detailFavorites
            downloads: downloadUi
            onDownloadPrepared: function(token) { downloadRequestDialog.showRequest(token) }
            onPoolsRequested: function(key, revision) { poolNavigation.requestDetailMemberships(key, revision) }
            complexQueryActionsEnabled: true
            onBackRequested: root.returnToGallery()
            onImportTagsRequested: tagCsvDialog.open()
            onAddTagToQueryRequested: function(tagName) {
                root.addDetailTagToDraft(tagName)
            }
        }

        PoolPage {
            onPoolDownloadRequested: function(token) { poolDownloadRequestDialog.showRequest(token) }
            controller: poolBrowser
            navigation: poolNavigation
            cacheController: imageCacheController
            theme: root.theme
        }

        PoolReaderPage {
            onPoolDownloadRequested: function(token) { poolDownloadRequestDialog.showRequest(token) }
            controller: poolReader
            navigation: poolNavigation
            theme: root.theme
        }
    }

    PoolMembershipPanel {
        id: poolMembershipPanel
        controller: poolBrowser
        navigation: poolNavigation
        theme: root.theme
    }

    Connections {
        target: poolNavigation
        function onMembershipsRequested() { poolMembershipPanel.open() }
        function onPoolPageRequested() { root.activePage = "pool"; queryDrawer.close() }
        function onSourceRestored() { root.activePage = detailNavigation.sourcePage }
        function onReaderPageRequested() { root.activePage = "reader"; queryDrawer.close() }
        function onSourcePageRequested(pageName) {
            root.activePage = pageName
            if (pageName === "favorites") favoritesPage.scheduleRestorePosition()
        }
    }

    Connections {
        target: poolReader
        function onDownloadPrepared(token) { downloadRequestDialog.showRequest(token) }
    }

    Connections {
        target: detailController
        function onOpenChanged() {
            if (detailController.isOpen)
                queryDrawer.close()
        }
    }

    Connections {
        target: detailNavigation
        function onTagSearchRequested(tagName) {
            root.startQueryFromDetailTag(tagName)
        }
    }

    Connections {
        target: sessionController
        function onGalleryRestoreReady() {
            Qt.callLater(root.restoreHistoryPosition)
        }
    }

    Shortcut {
        objectName: "detailEscapeShortcut"
        enabled: detailController.isOpen
        sequence: "Escape"
        onActivated: root.returnToGallery()
    }
}
