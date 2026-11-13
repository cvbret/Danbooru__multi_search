import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "cacheCleanupDialog"
    required property var controller
    property QtObject theme: AppTheme {}
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(500, parent ? parent.width - 32 : 500)
    modal: true
    title: qsTr("确认清理范围")
    onRejected: controller.cancelCleanup()
    contentItem: ColumnLayout {
        spacing: 16
        Label { objectName: "cacheCleanupDescription"; Layout.fillWidth: true; text: dialog.controller.confirmation.message; color: theme.text; wrapMode: Text.Wrap }
        Label { objectName: "cacheCleanupNotice"; Layout.fillWidth: true; visible: text.length > 0; text: dialog.controller.noticeText; color: theme.warning; wrapMode: Text.Wrap }
        Flow {
            Layout.fillWidth: true
            spacing: 8
            ActionButton { objectName: "cancelCacheCleanupButton"; text: qsTr("取消"); onClicked: { dialog.controller.cancelCleanup(); dialog.close() } }
            ActionButton {
                objectName: "confirmCacheCleanupButton"
                text: qsTr("确认清理")
                enabled: dialog.controller.canWrite && dialog.controller.confirmation.token.length > 0
                property string pressedToken: ""
                onPressed: pressedToken = dialog.controller.confirmation.token
                onClicked: { if (dialog.controller.confirmCleanup(pressedToken)) dialog.close(); pressedToken = "" }
            }
        }
    }
}
