import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: panel
    objectName: "eaglePanel"
    required property var controller
    property QtObject theme: AppTheme {}
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(720, parent ? parent.width - 24 : 720)
    height: Math.min(700, parent ? parent.height - 24 : 700)
    modal: true
    title: qsTr("Eagle 连接与目标文件夹")
    contentItem: ScrollView {
        id: bodyScroll
        objectName: "eaglePanelScroll"
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
        width: bodyScroll.availableWidth
        spacing: 10
        Label { Layout.fillWidth: true; text: qsTr("仅连接本机 Eagle。连接信息和目标仅在本次会话保留，打开面板不会发送请求。"); color: theme.secondaryText; wrapMode: Text.Wrap }
        Flow {
            Layout.fillWidth: true; spacing: 8
            ActionButton { objectName: "refreshEagleButton"; text: qsTr("测试连接 / 刷新"); enabled: panel.controller.supported && !panel.controller.busy; onClicked: panel.controller.refresh() }
            ActionButton { objectName: "cancelEagleButton"; text: qsTr("取消请求"); visible: panel.controller.busy; onClicked: panel.controller.cancel() }
            ActionButton { objectName: "closeEaglePanelButton"; text: qsTr("关闭"); onClicked: panel.close() }
        }
        Label { objectName: "eagleStatus"; Layout.fillWidth: true; text: panel.controller.statusText; color: panel.controller.uncertain ? theme.warning : theme.text; wrapMode: Text.Wrap }
        ScrollView {
            id: errorScroll
            Layout.fillWidth: true; Layout.preferredHeight: visible ? 62 : 0; visible: panel.controller.detailsText.length > 0
            contentWidth: availableWidth; clip: true
            Label { width: errorScroll.availableWidth; text: panel.controller.detailsText; color: theme.secondaryText; wrapMode: Text.WrapAnywhere }
        }
        Label { Layout.fillWidth: true; visible: panel.controller.ready; text: qsTr("Eagle %1 %2 · 素材库：%3").arg(panel.controller.library.version).arg(panel.controller.library.build).arg(panel.controller.library.name); color: theme.text; wrapMode: Text.Wrap }
        Label { Layout.fillWidth: true; visible: panel.controller.ready; text: panel.controller.library.path; color: theme.secondaryText; wrapMode: Text.WrapAnywhere }
        Label { Layout.fillWidth: true; text: qsTr("本次目标：%1").arg(panel.controller.target.name); color: theme.text; wrapMode: Text.Wrap; font.bold: true }
        ListView {
            objectName: "eagleFolderList"
            visible: panel.controller.ready
            Layout.fillWidth: true; Layout.preferredHeight: visible ? 160 : 0; Layout.minimumHeight: visible ? 64 : 0
            model: visible ? panel.controller.folders : null; spacing: 5; clip: true
            ScrollBar.vertical: ScrollBar {}
            delegate: ActionButton {
                required property var modelData
                objectName: "eagleFolder_" + modelData.key
                width: ListView.view.width
                text: modelData.name
                checkable: true; checked: panel.controller.target.key === modelData.key
                enabled: panel.controller.ready && !panel.controller.busy
                property var pressedAction: null
                onPressed: pressedAction = {"key": modelData.key, "revision": panel.controller.revision}
                onClicked: { if (pressedAction) panel.controller.selectTarget(pressedAction.key, pressedAction.revision); pressedAction = null }
            }
        }
        Label { Layout.fillWidth: true; text: qsTr("在所选目标内新建文件夹"); color: theme.text; font.bold: true }
        RowLayout {
            Layout.fillWidth: true
            TextField { id: folderName; objectName: "newEagleFolderName"; Layout.fillWidth: true; placeholderText: qsTr("输入新文件夹名称"); enabled: panel.controller.ready && !panel.controller.busy }
            ActionButton {
                objectName: "createEagleFolderButton"; text: qsTr("新建文件夹")
                enabled: panel.controller.ready && !panel.controller.busy && panel.controller.target.valid && folderName.text.trim().length > 0
                property var pressedAction: null
                onPressed: pressedAction = {"name": folderName.text, "parent": panel.controller.target.key, "revision": panel.controller.revision}
                onClicked: { if (pressedAction) panel.controller.createFolder(pressedAction.name, pressedAction.parent, pressedAction.revision); pressedAction = null }
            }
        }
        Label { Layout.fillWidth: true; text: qsTr("新建操作会写入 Eagle 当前素材库。请求发出后，取消无法撤销创建；结果不确定时请先刷新查看。"); color: theme.secondaryText; wrapMode: Text.Wrap }
        }
    }
}
