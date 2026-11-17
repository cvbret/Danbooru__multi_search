import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    objectName: "poolReaderPage"
    required property var controller
    required property var navigation
    signal poolDownloadRequested(string token)
    property QtObject theme: AppTheme {}
    onVisibleChanged: {
        controller.setActive(visible)
        poolDownloads.setSourceVisible("reader", visible)
    }
    property bool positioning: false

    function positionPage(number) {
        if (!visible) return
        positioning = true
        const revision = controller.revision
        const session = controller.readingSession
        Qt.callLater(function() {
            if (page.visible && controller.active && session === controller.readingSession
                    && revision === controller.revision && number === controller.currentPage) {
                if (controller.mode === "continuous") pages.forceLayout()
                const plan = controller.preparePosition(body.width, body.height, session, revision)
                if (plan.valid) {
                    if (controller.mode === "continuous") {
                        pages.forceLayout()
                        pages.positionViewAtIndex(number - 1, ListView.Beginning)
                        pages.currentIndex = number - 1
                        const row = pages.itemAtIndex(number - 1)
                        if (row) pages.contentY = row.y + plan.offset
                    }
                    controller.confirmPosition(session, revision)
                }
            }
            if (session === controller.readingSession && revision === controller.revision)
                page.positioning = false
        })
    }
    Connections {
        target: controller
        function onPositionRequested(number) { page.positionPage(number) }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 10
        Flow {
            Layout.fillWidth: true; spacing: 8
            ActionButton { objectName: "readerBackButton"; text: qsTr("返回作品池"); onClicked: navigation.leaveReader() }
            ActionButton {
                objectName: "readerDownloadAllButton"; text: qsTr("下载整个作品池")
                enabled: controller.totalPages > 0
                property string pressedToken: ""
                onPressed: pressedToken = poolDownloads.capturePool("reader", String(controller.readingSession))
                onClicked: { if (pressedToken.length) page.poolDownloadRequested(pressedToken) }
            }
            ActionButton { objectName: "readerContinuousButton"; text: qsTr("连续阅读"); checkable: true; checked: controller.mode === "continuous"; onClicked: controller.setMode("continuous", controller.revision) }
            ActionButton { objectName: "readerSingleButton"; text: qsTr("单页阅读"); checkable: true; checked: controller.mode === "single"; onClicked: controller.setMode("single", controller.revision) }
            ActionButton { objectName: "readerDirectoryButton"; text: qsTr("页面目录"); onClicked: directory.open() }
            ActionButton {
                objectName: "readerRestartButton"; text: qsTr("从第一页重新开始")
                property int pressedRevision: -1
                onPressed: pressedRevision = controller.revision
                onClicked: controller.restartFromBeginning(pressedRevision)
            }
            ActionButton {
                objectName: "readerRetryProgressButton"
                text: qsTr("重试读取 / 保存进度")
                visible: controller.progressStatus.length > 0
                property var pressedAction: null
                onPressed: pressedAction = {"session": controller.readingSession, "revision": controller.revision}
                onClicked: {
                    if (pressedAction && pressedAction.session === controller.readingSession
                            && pressedAction.revision === controller.revision) controller.retryProgress()
                    pressedAction = null
                }
            }
            ActionButton { objectName: "readerPreviousButton"; text: qsTr("上一页"); enabled: controller.currentPage > 1; onClicked: controller.goToPage(controller.currentPage - 1, controller.revision) }
            Label { height: 40; verticalAlignment: Text.AlignVCenter; text: qsTr("%1 / %2 页").arg(controller.totalPages ? controller.currentPage : 0).arg(controller.totalPages); color: theme.text }
            ActionButton { objectName: "readerNextButton"; text: qsTr("下一页"); enabled: controller.currentPage < controller.totalPages; onClicked: controller.goToPage(controller.currentPage + 1, controller.revision) }
            TextField {
                id: pageInput
                objectName: "readerJumpInput"
                width: 90
                placeholderText: qsTr("页码")
                validator: IntValidator { bottom: 1; top: controller.totalPages }
                onAccepted: controller.goToPage(Number(text), controller.revision)
            }
            ActionButton { objectName: "readerJumpButton"; text: qsTr("跳转"); onClicked: controller.goToPage(Number(pageInput.text), controller.revision) }
        }
        Flow {
            Layout.fillWidth: true; spacing: 8
            ActionButton { objectName: "readerFitWidthButton"; text: qsTr("适应宽度"); onClicked: controller.fit("fit-width", controller.revision) }
            ActionButton { objectName: "readerFitWindowButton"; text: qsTr("适应窗口"); onClicked: controller.fit("fit-window", controller.revision) }
            ActionButton { objectName: "readerZoomOutButton"; text: "−"; onClicked: controller.setZoom(Math.max(0.05, controller.zoom / 1.15), controller.revision) }
            Label { text: Math.round(controller.zoom * 100) + "%"; height: 40; verticalAlignment: Text.AlignVCenter; color: theme.secondaryText }
            ActionButton { objectName: "readerZoomInButton"; text: "+"; onClicked: controller.setZoom(Math.min(8, controller.zoom * 1.15), controller.revision) }
            ActionButton {
                objectName: "readerFavoriteButton"
                text: controller.favoriteState.saved ? qsTr("取消本地收藏") : qsTr("本地收藏当前页")
                enabled: controller.favoriteState.enabled
                property var pressedAction: null
                onPressed: pressedAction = {"key": controller.current.pageKey, "revision": controller.revision, "desired": !controller.favoriteState.saved}
                onClicked: { if (pressedAction) controller.setFavorite(pressedAction.key, pressedAction.revision, pressedAction.desired); pressedAction = null }
            }
            ActionButton {
                objectName: "readerDownloadButton"; text: qsTr("下载当前页原文件")
                enabled: controller.current.pageState === "available"
                property var pressedAction: null
                onPressed: pressedAction = {"key": controller.current.pageKey, "revision": controller.revision}
                onClicked: { if (pressedAction) controller.prepareDownload(pressedAction.key, pressedAction.revision); pressedAction = null }
            }
            ActionButton { objectName: "readerDetailButton"; text: qsTr("作品详情"); enabled: controller.current.pageState === "available"; property var pressedAction: null; onPressed: pressedAction = {"key": controller.current.pageKey, "revision": controller.revision}; onClicked: { if (pressedAction) controller.openDetail(pressedAction.key, pressedAction.revision); pressedAction = null } }
            ActionButton { objectName: "readerRetryButton"; text: qsTr("重试当前页"); property var pressedAction: null; onPressed: pressedAction = {"key": controller.current.pageKey, "revision": controller.revision}; onClicked: { if (pressedAction) controller.retryPage(pressedAction.key, pressedAction.revision); pressedAction = null } }
        }
        Label { Layout.fillWidth: true; visible: text.length > 0; text: controller.errorText || downloadUi.errorText; color: theme.error; wrapMode: Text.Wrap }
        Item {
            id: body
            onWidthChanged: if (page.visible && controller) controller.changeViewport(width, height, controller.readingSession)
            onHeightChanged: if (page.visible && controller) controller.changeViewport(width, height, controller.readingSession)
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 140
            ListView {
                id: pages
                objectName: "readerContinuousList"
                anchors.fill: parent
                visible: controller.mode === "continuous"
                model: controller.active && controller.mode === "continuous" ? controller.model : null
                clip: true
                cacheBuffer: height
                contentWidth: width * Math.max(1, controller.zoom)
                flickableDirection: Flickable.AutoFlickDirection
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}
                ScrollBar.horizontal: ScrollBar {}
                onContentYChanged: {
                    if (!page.positioning && visible && page.visible) {
                        const index = indexAt(1, contentY + Math.min(60, height / 3))
                        if (index >= 0 && moving) controller.observePage(index + 1)
                        const row = itemAtIndex(controller.currentPage - 1)
                        if (row) controller.rememberScroll(controller.currentPage, contentY - row.y)
                    }
                }
                delegate: PoolReaderItem {
                    width: pages.contentWidth
                    readingWidth: pages.width; viewportHeight: pages.height
                    controller: page.controller; theme: page.theme
                }
            }
            Item {
                anchors.fill: parent
                visible: controller.mode === "single"
                Repeater {
                    model: page.visible && controller.mode === "single" ? [controller.currentPage - 1, controller.currentPage, controller.currentPage + 1].filter(function(n) { return n >= 1 && n <= controller.totalPages }) : []
                    PoolReaderItem {
                        required property int modelData
                        pageNumber: modelData
                        anchors.fill: parent
                        readingWidth: width; viewportHeight: height
                        controller: page.controller; theme: page.theme; single: true
                        visible: pageNumber === controller.currentPage
                    }
                }
            }
            Label { anchors.centerIn: parent; visible: controller.totalPages === 0; text: qsTr("作品池没有页面。"); color: theme.secondaryText }
        }
    }
    Drawer {
        id: directory
        objectName: "readerDirectory"
        parent: Overlay.overlay
        edge: Qt.RightEdge
        width: Math.min(420, page.width * 0.9); height: parent ? parent.height : page.height
        padding: 16; modal: true
        onOpened: controller.setDirectoryOpen(true)
        onClosed: controller.setDirectoryOpen(false)
        ColumnLayout {
            anchors.fill: parent; spacing: 10
            Label { text: qsTr("原始页面目录"); font.bold: true; color: theme.text }
            ListView {
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                model: controller.directoryRows
                delegate: ItemDelegate {
                    required property var modelData
                    width: ListView.view.width; height: 96
                    objectName: "readerDirectoryPage_" + modelData.pageNumber
                    contentItem: RowLayout {
                        Image { Layout.preferredWidth: 80; Layout.fillHeight: true; source: directory.opened && page.visible ? modelData.previewUrl : ""; fillMode: Image.PreserveAspectFit; asynchronous: true }
                        Label { Layout.fillWidth: true; text: qsTr("第 %1 页 · #%2").arg(modelData.pageNumber).arg(modelData.postKey) + "\n" + modelData.pageMessage; wrapMode: Text.Wrap; color: theme.text }
                    }
                    property var pressedAction: null
                    onPressed: pressedAction = {"page": modelData.pageNumber, "revision": controller.revision}
                    onClicked: { if (pressedAction && controller.goToPage(pressedAction.page, pressedAction.revision)) directory.close(); pressedAction = null }
                }
            }
            Flow {
                Layout.fillWidth: true; spacing: 8
                ActionButton { objectName: "readerDirectoryPreviousButton"; text: qsTr("上一组"); enabled: controller.directoryStart > 1; onClicked: controller.showDirectory(Math.max(1, controller.directoryStart - 5), controller.revision) }
                ActionButton { objectName: "readerDirectoryNextButton"; text: qsTr("下一组"); enabled: controller.directoryStart + 5 <= controller.totalPages; onClicked: controller.showDirectory(controller.directoryStart + 5, controller.revision) }
                ActionButton { objectName: "readerDirectoryCloseButton"; text: qsTr("关闭"); onClicked: directory.close() }
            }
        }
    }
    Connections { target: controller; function onChanged() { if (!controller.active) directory.close() } }
}
