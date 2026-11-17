import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "downloadRequestDialog"
    required property var controller
    property QtObject theme: AppTheme {}
    property string requestToken: ""
    property string summaryText: qsTr("将下载 %1 项作品的原始文件。").arg(controller.pending.count)
    property string explanationText: qsTr("确认后保存下载任务。文件保持原始格式，取消此窗口不会创建任务。")
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(600, parent ? parent.width - 32 : 600)
    height: Math.min(540, parent ? parent.height - 32 : 540)
    modal: true
    title: qsTr("下载原文件")
    closePolicy: controller.busy ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    onClosed: controller.cancelRequest(requestToken)

    function showRequest(token) {
        if (!controller.openRequest(token))
            return
        requestToken = token
        policy.currentIndex = 0
        saveDefault.checked = false
        open()
    }

    FolderDialog {
        id: destinationDialog
        objectName: "downloadDestinationFolderDialog"
        property string choiceToken: ""
        title: qsTr("选择本次下载目录")
        currentFolder: dialog.controller.pending.directoryUrl
        function acceptSelection(folder) { dialog.controller.acceptDirectory(folder, choiceToken) }
        onAccepted: acceptSelection(selectedFolder)
        onRejected: dialog.controller.cancelDirectorySelection(choiceToken)
    }

    contentItem: ScrollView {
        objectName: "downloadRequestScroll"
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            width: parent.width
            spacing: 16
            Label { Layout.fillWidth: true; text: dialog.summaryText; font.bold: true; color: theme.text; wrapMode: Text.Wrap }
            Label { Layout.fillWidth: true; text: dialog.explanationText; color: theme.secondaryText; wrapMode: Text.Wrap }
            Label { text: qsTr("本次下载目录"); font.bold: true; color: theme.text }
            Label { objectName: "downloadRequestDirectory"; Layout.fillWidth: true; text: dialog.controller.pending.directory; color: theme.secondaryText; wrapMode: Text.WrapAnywhere }
            ActionButton {
                objectName: "chooseDownloadDirectoryButton"
                text: qsTr("选择其他文件夹")
                enabled: !dialog.controller.busy && dialog.controller.pending.valid
                onClicked: {
                    destinationDialog.choiceToken = dialog.controller.beginDirectorySelection("request")
                    if (destinationDialog.choiceToken.length) destinationDialog.open()
                }
            }
            Label { text: qsTr("遇到同名文件时"); font.bold: true; color: theme.text }
            ComboBox {
                id: policy
                objectName: "downloadConflictPolicy"
                Layout.fillWidth: true
                enabled: !dialog.controller.busy
                textRole: "label"; valueRole: "value"
                model: [{"value":"skip","label":qsTr("跳过已有文件")},{"value":"overwrite","label":qsTr("覆盖已有文件")},{"value":"auto_number","label":qsTr("自动编号，保留已有文件")}]
            }
            Label {
                objectName: "downloadOverwriteExplanation"
                Layout.fillWidth: true
                text: policy.currentValue === "overwrite" ? qsTr("覆盖会用新下载完成的文件替换同名文件。下载失败或取消时保留旧文件。")
                      : policy.currentValue === "auto_number" ? qsTr("同名时使用新的编号保存，已有文件保持不变。") : qsTr("发现同名文件时记录为“已跳过”，不会重新下载。")
                color: policy.currentValue === "overwrite" ? theme.warning : theme.secondaryText
                wrapMode: Text.Wrap
            }
            CheckBox { id: saveDefault; objectName: "saveDownloadDirectoryDefault"; text: qsTr("同时将本次目录保存为默认下载目录"); enabled: !dialog.controller.busy }
            Label { objectName: "downloadRequestError"; Layout.fillWidth: true; text: dialog.controller.errorText; visible: text.length > 0; color: theme.error; wrapMode: Text.Wrap }
            Label { Layout.fillWidth: true; text: qsTr("作品来源已经改变，请取消后重新选择。"); visible: !dialog.controller.pending.valid; color: theme.error; wrapMode: Text.Wrap }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ActionButton { objectName: "cancelDownloadRequestButton"; text: qsTr("取消"); enabled: !dialog.controller.busy; onClicked: dialog.close() }
                ActionButton {
                    objectName: "confirmDownloadRequestButton"
                    text: qsTr("确认并开始下载")
                    primary: true
                    enabled: !dialog.controller.busy && dialog.controller.pending.valid
                    property var pressedAction: null
                    onPressed: pressedAction = {"token": dialog.requestToken, "policy": policy.currentValue, "saveDefault": saveDefault.checked}
                    onClicked: {
                        if (pressedAction && dialog.controller.confirmRequest(pressedAction.token, pressedAction.policy, pressedAction.saveDefault)) dialog.close()
                        pressedAction = null
                    }
                }
            }
        }
    }
}
