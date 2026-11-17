import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    objectName: "poolPage"
    required property var controller
    required property var navigation
    required property var cacheController
    property QtObject theme: AppTheme {}
    ColumnLayout {
        anchors.fill: parent
        spacing: 12
        Flow {
            Layout.fillWidth: true; spacing: 8
            ActionButton {
                objectName: "poolReturnSourceButton"
                text: navigation.sourceLabel
                property string pressedToken: ""
                onPressed: pressedToken = navigation.returnToken
                onClicked: navigation.returnToSource(pressedToken)
            }
            ActionButton { objectName: "poolReloadButton"; text: qsTr("重新读取"); onClicked: controller.retryWindow() }
            ActionButton { objectName: "poolCancelButton"; text: qsTr("取消请求"); visible: controller.busy; onClicked: controller.cancel() }
            ActionButton {
                objectName: "poolFavoriteButton"
                visible: controller.poolInfo.category === "series"
                enabled: controller.poolFavoriteState.enabled
                text: !controller.poolFavoriteState.known ? qsTr("收藏状态未知")
                      : controller.poolFavoriteState.saved ? qsTr("取消本地收藏") : qsTr("收藏系列作品池")
                property var pressedAction: null
                onPressed: pressedAction = {"generation": controller.generation, "desired": !controller.poolFavoriteState.saved}
                onClicked: { if (pressedAction) controller.setPoolFavorite(pressedAction.generation, pressedAction.desired); pressedAction = null }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: false
            Layout.minimumHeight: 0
            Layout.preferredHeight: Math.min(150, page.height * 0.25)
            Layout.maximumHeight: Layout.preferredHeight
            Rectangle {
                Layout.preferredWidth: 120; Layout.fillHeight: true
                color: theme.card; radius: 8; border.color: theme.border
                Loader {
                    anchors.fill: parent; anchors.margins: 4
                    active: page.visible && controller.coverUrl.length > 0
                    sourceComponent: Image { source: controller.coverUrl; fillMode: Image.PreserveAspectFit; asynchronous: true }
                }
                Label { anchors.centerIn: parent; visible: !controller.coverUrl.length; text: qsTr("暂无封面"); color: theme.secondaryText }
            }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true; contentWidth: availableWidth; clip: true
                ColumnLayout {
                    width: parent.width; spacing: 8
                    Label { Layout.fillWidth: true; text: controller.poolInfo.name || qsTr("读取作品池"); font.pixelSize: 24; font.bold: true; wrapMode: Text.Wrap; color: theme.text }
                    Label { Layout.fillWidth: true; text: (controller.poolInfo.categoryText || "") + qsTr(" · 共 %1 页位").arg(controller.poolInfo.postCount || 0); wrapMode: Text.Wrap; color: theme.secondaryText }
                    Label { Layout.fillWidth: true; text: controller.poolInfo.description || qsTr("没有说明"); wrapMode: Text.Wrap; color: theme.secondaryText }
                }
            }
        }
        Label { objectName: "poolPageError"; Layout.fillWidth: true; text: controller.errorText || favoriteController.noticeText; visible: text.length > 0; wrapMode: Text.Wrap; color: theme.error }
        Flow {
            Layout.fillWidth: true; spacing: 8
            Label { height: 40; verticalAlignment: Text.AlignVCenter; text: qsTr("原始页位 %1—%2 · 每批最多40页").arg(controller.model.count ? controller.firstPage : 0).arg(controller.firstPage + controller.model.count - 1); color: theme.secondaryText }
            ActionButton { objectName: "poolPreviousWindowButton"; text: qsTr("上一批"); enabled: controller.firstPage > 1; onClicked: controller.showWindow(Math.max(1, controller.firstPage - 40), controller.generation) }
            ActionButton { objectName: "poolNextWindowButton"; text: qsTr("下一批"); enabled: controller.firstPage + 40 <= (controller.poolInfo.postCount || 0); onClicked: controller.showWindow(controller.firstPage + 40, controller.generation) }
        }
        GridView {
            id: grid
            objectName: "poolPageGrid"
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.minimumHeight: 120
            model: controller.model; clip: true
            cellWidth: Math.max(225, width / Math.max(1, Math.floor(width / 245)))
            cellHeight: 340
            ScrollBar.vertical: ScrollBar {}
            delegate: Loader {
                id: row
                required property int index
                readonly property var value: { const revision = controller.model.revision; return controller.rowAt(index) }
                width: grid.cellWidth - 12; height: grid.cellHeight - 12
                sourceComponent: value.state === "available" ? available : unavailable
                Component {
                    id: available
                    PostCard {
                        width: row.width; height: row.height
                        index: row.index; postKey: row.value.postKey; selectionKey: row.value.pageKey
                        titleText: qsTr("第 %1 页 · #%2").arg(row.value.pageNumber).arg(row.value.postKey)
                        actionRevision: controller.model.revision; selected: false; selectionActionVisible: false
                        mediaActive: page.visible
                        readonly property var stateValue: { const generation = controller.generation; const busy = favoriteController.busy; const revision = controller.model.revision; return controller.postState(row.value.pageKey) }
                        favoriteKnown: stateValue.known; favoriteSaved: stateValue.saved; favoriteEnabled: stateValue.enabled
                        previewUrl: row.value.previewUrl; scoreText: row.value.scoreText; favoriteText: row.value.favoriteText
                        ratingCode: row.value.ratingCode; ratingText: row.value.ratingText; dimensionsText: row.value.dimensionsText
                        fileExtension: row.value.fileExtension; mediaLabel: row.value.mediaLabel
                        cacheController: page.cacheController; theme: page.theme
                        onFavoriteRequested: function(key, desired, revision) { controller.setPostFavorite(key, desired, revision) }
                        onDetailRequested: function(key, revision, rowIndex) { navigation.openPage(key, revision) }
                    }
                }
                Component {
                    id: unavailable
                    Rectangle {
                        color: theme.card; radius: 9; border.color: theme.border
                        ColumnLayout {
                            anchors.fill: parent; anchors.margins: 16; spacing: 12
                            Label { Layout.fillWidth: true; text: qsTr("第 %1 页 · #%2").arg(row.value.pageNumber).arg(row.value.postKey); font.bold: true; color: theme.text; wrapMode: Text.Wrap }
                            Label { Layout.fillWidth: true; text: row.value.state === "loading" ? qsTr("正在读取页面……") : row.value.message; wrapMode: Text.Wrap; color: theme.secondaryText }
                            Item { Layout.fillHeight: true }
                            ActionButton {
                                objectName: "retryPoolPage_" + row.value.pageKey
                                text: qsTr("重试这一页"); enabled: !controller.busy
                                property var pressedAction: null
                                onPressed: pressedAction = {"key": row.value.pageKey, "revision": controller.model.revision}
                                onClicked: { if (pressedAction) controller.retryPage(pressedAction.key, pressedAction.revision); pressedAction = null }
                            }
                        }
                    }
                }
            }
            Label { anchors.centerIn: parent; visible: controller.model.count === 0 && !controller.busy && !controller.errorText.length; text: qsTr("这个作品池没有页面。"); color: theme.secondaryText }
        }
    }
}
