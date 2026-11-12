import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    required property var controller
    property QtObject theme: AppTheme {}
    property string frozenToken: ""
    objectName: "favoriteConfirmDialog"
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(520, parent ? parent.width - 32 : 520)
    modal: true
    title: qsTr("确认本地收藏操作")
    closePolicy: Popup.CloseOnEscape
    onOpened: frozenToken = controller.confirmation.token
    onRejected: controller.cancelConfirmation(frozenToken)
    contentItem: ColumnLayout {
        spacing: 20
        Label {
            objectName: "favoriteConfirmationText"
            Layout.fillWidth: true
            text: dialog.controller.confirmation.text
            color: theme.text
            wrapMode: Text.Wrap
        }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            ActionButton {
                objectName: "cancelFavoriteOperationButton"
                text: qsTr("取消")
                onClicked: dialog.controller.cancelConfirmation(dialog.frozenToken)
            }
            ActionButton {
                objectName: "confirmFavoriteOperationButton"
                text: qsTr("确认操作")
                enabled: dialog.frozenToken.length > 0 && !favoriteController.busy
                onClicked: dialog.controller.confirm(dialog.frozenToken)
            }
        }
    }
    Connections {
        target: dialog.controller
        function onConfirmationChanged() {
            if (dialog.controller.confirmation.token.length)
                dialog.open()
            else
                dialog.close()
        }
    }
}
