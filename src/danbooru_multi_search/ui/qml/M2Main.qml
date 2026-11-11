import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: root
    visible: true
    x: sessionController.initialWindowX
    y: sessionController.initialWindowY
    width: sessionController.initialWindowWidth
    height: sessionController.initialWindowHeight
    minimumWidth: sessionController.minimumWindowWidth
    minimumHeight: sessionController.minimumWindowHeight
    title: qsTr("Danbooru Multi Search")
    color: "#121419"

    property bool compactLayout: width < 980
    property real queryPanelWidth: 360
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
        if (!detailNavigation.closeDetail())
            return
        if (detailNavigation.sourcePage === "browse")
            Qt.callLater(root.restoreGallerySnapshot)
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
        if (!searchController.applyAdvancedText(tagName))
            return
        searchController.executeStandard()
    }

    function addDetailTagToDraft(tagName) {
        searchController.addVisualTag(null, tagName)
    }

    function ratingColor(code) {
        if (code === "g") return "#3f8f5a"
        if (code === "s") return "#3976b8"
        if (code === "q") return "#bb7628"
        if (code === "e") return "#b64056"
        return "#626872"
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
            color: "#20242c"
            border.color: "#4b5362"
            radius: 10
        }

        contentItem: ColumnLayout {
            spacing: 10
            Label {
                text: qsTr("图片缓存")
                color: "#f4f4f5"
                font.pixelSize: 20
                font.bold: true
            }
            Label {
                objectName: "imageCacheUsageLabel"
                text: qsTr("当前占用：%1 MiB")
                      .arg((imageCacheController.currentBytes / 1048576).toFixed(2))
                color: "#d4d4d8"
            }
            RowLayout {
                Layout.fillWidth: true
                Label { text: qsTr("上限（MiB）"); color: "#d4d4d8" }
                SpinBox {
                    id: cacheLimitInput
                    objectName: "imageCacheLimitInput"
                    from: 64
                    to: 16384
                    value: 512
                    editable: true
                    Layout.fillWidth: true
                }
                Button {
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
                color: "#fbbf24"
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Button {
                    objectName: "clearImageCacheButton"
                    text: qsTr("清理图片缓存")
                    onClicked: imageCacheController.clearCache()
                }
                Item { Layout.fillWidth: true }
                Button { text: qsTr("关闭"); onClicked: imageCachePopup.close() }
            }
        }
    }

    Component {
        id: queryWorkspaceComponent

        Rectangle {
            id: workspace
            color: "#181b22"
            border.color: "#303540"
            radius: 10

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                spacing: 10

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        Layout.fillWidth: true
                        text: qsTr("查询工作区")
                        color: "#f4f4f5"
                        font.pixelSize: 19
                        font.bold: true
                    }
                    Button {
                        objectName: "importTagsButton"
                        text: tagSuggestionController.importBusy
                              ? qsTr("导入中") : qsTr("标签库")
                        enabled: !tagSuggestionController.importBusy
                        onClicked: tagCsvDialog.open()
                    }
                    Button {
                        objectName: "imageCacheButton"
                        text: qsTr("缓存")
                        onClicked: imageCachePopup.open()
                    }
                }

                Label {
                    objectName: "tagImportStatus"
                    Layout.fillWidth: true
                    text: tagSuggestionController.importStatusText
                    color: tagSuggestionController.importBusy ? "#93c5fd" : "#71717a"
                    elide: Text.ElideRight
                    font.pixelSize: 11
                }

                TabBar {
                    id: workspaceTabs
                    objectName: "workspaceTabs"
                    Layout.fillWidth: true
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
                        controller: searchController
                        tagController: tagSuggestionController
                        onCloseRequested: {
                            if (root.compactLayout)
                                queryDrawer.close()
                        }
                    }
                    QueryFilterPanel { controller: searchController }
                    HistoryPanel { sessionController: root.browserSession }
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
        visible: !root.compactLayout
        width: visible ? root.queryPanelWidth : 0
        anchors.left: parent.left
        anchors.top: parent.top
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
                   ? "#60a5fa" : "transparent"
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
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.margins: 10
        currentIndex: detailController.isOpen ? 1 : 0

        Item {
            id: galleryPage

            ColumnLayout {
                anchors.fill: parent
                spacing: 10

                RowLayout {
                    Layout.fillWidth: true
                    Button {
                        objectName: "openQueryDrawerButton"
                        visible: root.compactLayout
                        text: qsTr("查询条件")
                        onClicked: queryDrawer.open()
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 1
                        Label {
                            text: qsTr("Danbooru 作品浏览器")
                            color: "#f4f4f5"
                            font.pixelSize: 22
                            font.bold: true
                        }
                        Label {
                            text: searchController.hasDisplayedGallery
                                  ? (searchController.galleryExpression.length > 0
                                     ? searchController.galleryExpression
                                     : searchController.galleryMode === "rank"
                                       ? qsTr("近期热门") : qsTr("最新作品"))
                                  : qsTr("尚未执行查询")
                            color: "#a1a1aa"
                            elide: Text.ElideRight
                        }
                    }
                }

                QueryStatusBar {
                    id: queryStatus
                    objectName: "queryStatusBar"
                    Layout.fillWidth: true
                    controller: searchController
                    sessionController: root.browserSession
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: 300
                    radius: 10
                    color: "#0f1115"
                    border.color: "#2d3139"
                    clip: true

                    GridView {
                        id: galleryGrid
                        objectName: "galleryGrid"
                        anchors.fill: parent
                        anchors.margins: 10
                        clip: true
                        model: searchController.postsModel
                        cellWidth: Math.max(
                            225,
                            width / Math.max(1, Math.floor(width / 245))
                        )
                        cellHeight: 340
                        boundsBehavior: Flickable.StopAtBounds
                        ScrollBar.vertical: ScrollBar {}
                        onContentYChanged: root.reportGalleryPosition()
                        onCurrentIndexChanged: root.reportGalleryPosition()
                        onWidthChanged: Qt.callLater(root.reportGalleryPosition)
                        onHeightChanged: Qt.callLater(root.reportGalleryPosition)
                        onContentHeightChanged: Qt.callLater(root.reportGalleryPosition)

                        delegate: Rectangle {
                            id: card
                            objectName: "postCard_" + postId
                            required property int index
                            required property int postId
                            required property string previewUrl
                            required property string scoreText
                            required property string favoriteText
                            required property string ratingCode
                            required property string ratingText
                            required property string dimensionsText
                            required property string fileExtension
                            required property string mediaLabel
                            property int thumbnailRebuildToken: 0
                            property bool thumbnailFailed: false
                            property bool thumbnailLoading: false

                            width: GridView.view.cellWidth - 12
                            height: GridView.view.cellHeight - 12
                            radius: 9
                            color: "#191c23"
                            border.color: "#343944"

                            function rebuildThumbnail() {
                                const token = ++thumbnailRebuildToken
                                thumbnailFailed = false
                                thumbnailLoading = card.previewUrl.length > 0
                                thumbnailLoader.active = false
                                Qt.callLater(function() {
                                    if (token !== thumbnailRebuildToken)
                                        return
                                    thumbnailLoader.active = card.previewUrl.length > 0
                                })
                            }

                            function handleThumbnailStatus(
                                    token, url, cacheGeneration, status) {
                                if (token !== thumbnailRebuildToken
                                        || url !== card.previewUrl)
                                    return
                                if (status === Image.Ready) {
                                    thumbnailLoading = false
                                    const action = imageCacheController
                                        .decodeSuccessActionForGeneration(
                                            url, cacheGeneration)
                                    thumbnailFailed = action !== "accepted"
                                } else if (status === Image.Error) {
                                    thumbnailLoading = false
                                    const finalUrl = imageCacheController.finalUrlFor(url)
                                    const action = imageCacheController
                                        .decodeFailureActionForGeneration(
                                            url, finalUrl, cacheGeneration)
                                    if (action === "retry")
                                        rebuildThumbnail()
                                    else
                                        thumbnailFailed = true
                                }
                            }

                            function retryThumbnail() {
                                imageCacheController.prepareManualRetry(
                                    card.previewUrl,
                                    imageCacheController.finalUrlFor(card.previewUrl)
                                )
                                rebuildThumbnail()
                            }

                            Component.onCompleted: rebuildThumbnail()
                            onPreviewUrlChanged: rebuildThumbnail()
                            Component.onDestruction: {
                                ++thumbnailRebuildToken
                                thumbnailLoader.active = false
                            }

                            TapHandler { onTapped: root.openPostDetails(card.index) }

                            Loader {
                                id: thumbnailLoader
                                objectName: "thumbnailLoader_" + card.postId
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 8
                                height: 220
                                active: false
                                asynchronous: false
                                sourceComponent: thumbnailComponent
                            }

                            Component {
                                id: thumbnailComponent
                                Image {
                                    objectName: "thumbnail_" + card.postId
                                    anchors.fill: parent
                                    source: ""
                                    fillMode: Image.PreserveAspectFit
                                    asynchronous: true
                                    cache: false
                                    property int frozenToken: 0
                                    property int frozenCacheGeneration: 0
                                    property string frozenUrl: ""
                                    Component.onCompleted: {
                                        frozenToken = card.thumbnailRebuildToken
                                        frozenCacheGeneration = imageCacheController.cacheGeneration
                                        frozenUrl = card.previewUrl
                                        source = frozenUrl
                                    }
                                    onStatusChanged: card.handleThumbnailStatus(
                                        frozenToken,
                                        frozenUrl,
                                        frozenCacheGeneration,
                                        status
                                    )
                                }
                            }

                            BusyIndicator {
                                anchors.centerIn: thumbnailLoader
                                running: card.thumbnailLoading
                                visible: running
                            }

                            Rectangle {
                                objectName: "thumbnailError_" + card.postId
                                anchors.fill: thumbnailLoader
                                color: "#242832"
                                visible: card.thumbnailFailed
                                Column {
                                    anchors.centerIn: parent
                                    width: parent.width - 24
                                    spacing: 8
                                    Label {
                                        width: parent.width
                                        text: qsTr("此缩略图加载失败")
                                        color: "#fda4af"
                                        horizontalAlignment: Text.AlignHCenter
                                        wrapMode: Text.Wrap
                                    }
                                    Button {
                                        objectName: "thumbnailRetry_" + card.postId
                                        anchors.horizontalCenter: parent.horizontalCenter
                                        text: qsTr("重试")
                                        onClicked: card.retryThumbnail()
                                    }
                                }
                            }

                            Row {
                                anchors.left: parent.left
                                anchors.top: parent.top
                                anchors.margins: 12
                                spacing: 5
                                Rectangle {
                                    width: mediaText.implicitWidth + 12
                                    height: 24
                                    radius: 5
                                    color: "#a23f52"
                                    visible: card.mediaLabel.length > 0
                                    Label {
                                        id: mediaText
                                        anchors.centerIn: parent
                                        text: card.mediaLabel
                                        color: "white"
                                        font.bold: true
                                    }
                                }
                            }

                            Rectangle {
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 12
                                width: ratingLabel.implicitWidth + 12
                                height: 24
                                radius: 5
                                color: root.ratingColor(card.ratingCode)
                                Label {
                                    id: ratingLabel
                                    anchors.centerIn: parent
                                    text: card.ratingText
                                    color: "white"
                                    font.bold: true
                                }
                            }

                            ColumnLayout {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.margins: 10
                                spacing: 4
                                Label {
                                    Layout.fillWidth: true
                                    text: qsTr("作品 #%1").arg(card.postId)
                                    color: "#f4f4f5"
                                    font.bold: true
                                    elide: Text.ElideRight
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: qsTr("得分 %1 · 收藏 %2")
                                          .arg(card.scoreText).arg(card.favoriteText)
                                    color: "#d4d4d8"
                                    elide: Text.ElideRight
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: qsTr("%1 · %2")
                                          .arg(card.fileExtension).arg(card.dimensionsText)
                                    color: "#a1a1aa"
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }

                    Label {
                        objectName: "galleryEmptyState"
                        anchors.centerIn: parent
                        visible: searchController.postsModel.count === 0
                        width: Math.min(620, parent.width - 48)
                        text: queryStatus.emptyStateText
                        color: searchController.queryState === "failed"
                               ? "#fda4af" : "#a1a1aa"
                        font.pixelSize: 16
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Label {
                        objectName: "rangeDescription"
                        Layout.fillWidth: true
                        text: searchController.rangeDescription.length > 0
                              ? searchController.rangeDescription
                              : qsTr("范围说明会在取得结果后显示")
                        color: "#a1a1aa"
                        wrapMode: Text.Wrap
                    }
                    Button {
                        objectName: "refreshButton"
                        text: qsTr("刷新")
                        enabled: searchController.canRefresh
                        onClicked: searchController.refresh()
                    }
                    Button {
                        objectName: "retryButton"
                        text: qsTr("重试失败请求")
                        visible: searchController.canRetry
                        enabled: searchController.canRetry
                        onClicked: searchController.retryFailedPage()
                    }
                }
            }
        }

        DetailPage {
            controller: detailController
            complexQueryActionsEnabled: true
            onBackRequested: root.returnToGallery()
            onImportTagsRequested: tagCsvDialog.open()
            onAddTagToQueryRequested: function(tagName) {
                root.addDetailTagToDraft(tagName)
            }
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
