import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: panel
    objectName: "savedQueryPanel"
    required property var controller
    property QtObject theme: AppTheme {}
    signal dismissed()
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(700, parent ? parent.width - 32 : 700)
    height: Math.min(700, parent ? parent.height - 32 : 700)
    modal: true
    title: qsTr("常用查询")
    onAboutToShow: controller.setActive(true)
    onClosed: {
        renameDialog.close()
        deleteDialog.close()
        controller.setActive(false)
        dismissed()
    }

    Dialog {
        id: renameDialog
        objectName: "renameSavedQueryDialog"
        property var frozenAction: null
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, parent ? parent.width - 32 : 440)
        modal: true
        title: qsTr("修改常用查询名称")
        contentItem: ColumnLayout {
            spacing: 12
            TextField { id: renameInput; objectName: "savedQueryRenameInput"; Layout.fillWidth: true; placeholderText: qsTr("查询名称") }
            Label { Layout.fillWidth: true; text: panel.controller.errorText; visible: text.length > 0; color: theme.error; wrapMode: Text.Wrap }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ActionButton { text: qsTr("取消"); onClicked: renameDialog.close() }
                ActionButton {
                    objectName: "confirmSavedQueryRenameButton"
                    text: qsTr("保存名称")
                    enabled: panel.controller.canWrite && renameInput.text.trim().length > 0
                    onClicked: {
                        var action = renameDialog.frozenAction
                        if (action && panel.controller.rename(action.key, action.revision, action.generation, renameInput.text))
                            renameDialog.close()
                    }
                }
            }
        }
    }

    Dialog {
        id: deleteDialog
        objectName: "deleteSavedQueryDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, parent ? parent.width - 32 : 440)
        modal: true
        title: qsTr("删除常用查询")
        onRejected: panel.controller.cancelDelete()
        contentItem: ColumnLayout {
            spacing: 16
            Label { Layout.fillWidth: true; text: qsTr("删除常用查询“%1”？当前草稿和历史保持不变。").arg(panel.controller.deleteConfirmation.name); color: theme.text; wrapMode: Text.Wrap }
            Label { Layout.fillWidth: true; text: panel.controller.errorText; visible: text.length > 0; color: theme.error; wrapMode: Text.Wrap }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ActionButton { objectName: "cancelSavedQueryDeleteButton"; text: qsTr("取消"); onClicked: { panel.controller.cancelDelete(); deleteDialog.close() } }
                ActionButton {
                    objectName: "confirmSavedQueryDeleteButton"
                    text: qsTr("确认删除")
                    enabled: panel.controller.canWrite && panel.controller.deleteConfirmation.token.length > 0
                    property string pressedToken: ""
                    onPressed: pressedToken = panel.controller.deleteConfirmation.token
                    onClicked: { if (panel.controller.confirmDelete(pressedToken)) deleteDialog.close(); pressedToken = "" }
                }
            }
        }
    }

    Connections {
        target: panel.controller
        function onChanged() {
            if (!panel.controller.deleteConfirmation.token.length)
                deleteDialog.close()
        }
    }

    contentItem: ScrollView {
        objectName: "savedQueryScroll"
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: parent.width
            spacing: 16
            Label { Layout.fillWidth: true; text: qsTr("保存完整的查询条件。载入只修改草稿，点击查询按钮后才开始搜索。"); color: theme.secondaryText; wrapMode: Text.Wrap }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ActionButton { objectName: "captureSavedQueryDraftButton"; text: qsTr("使用当前查询条件"); onClicked: panel.controller.captureDraft() }
                ActionButton { objectName: "reloadSavedQueriesButton"; text: qsTr("刷新列表"); onClicked: panel.controller.reload() }
            }
            ColumnLayout {
                Layout.fillWidth: true
                visible: panel.controller.capturedDraft.token.length > 0
                spacing: 8
                Label { Layout.fillWidth: true; text: qsTr("待保存：%1 · %2").arg(panel.controller.capturedDraft.mode).arg(panel.controller.capturedDraft.expression); color: theme.text; wrapMode: Text.WrapAnywhere }
                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    TextField { id: newName; objectName: "savedQueryNameInput"; width: Math.min(300, panel.width - 56); placeholderText: qsTr("填写常用查询名称") }
                    ActionButton {
                        objectName: "saveCapturedQueryButton"
                        text: qsTr("保存常用查询")
                        primary: true
                        enabled: panel.controller.canWrite && newName.text.trim().length > 0 && panel.controller.capturedDraft.token.length > 0
                        property var pressedAction: null
                        onPressed: pressedAction = {"token": panel.controller.capturedDraft.token, "name": newName.text}
                        onClicked: { if (pressedAction && panel.controller.saveCaptured(pressedAction.token, pressedAction.name)) newName.clear(); pressedAction = null }
                    }
                }
            }
            Label { objectName: "savedQueryError"; Layout.fillWidth: true; visible: text.length > 0; text: panel.controller.errorText; color: theme.error; wrapMode: Text.Wrap }
            Label { Layout.fillWidth: true; visible: panel.controller.queries.length === 0; text: qsTr("还没有常用查询。使用当前查询条件，填写名称后保存。"); color: theme.mutedText; wrapMode: Text.Wrap }
            Repeater {
                model: panel.controller.queries
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: savedContent.implicitHeight + 24
                    color: theme.surface
                    radius: theme.smallRadius
                    border.color: theme.border
                    ColumnLayout {
                        id: savedContent
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 8
                        Label { Layout.fillWidth: true; text: modelData.name; color: theme.text; font.bold: true; wrapMode: Text.Wrap }
                        Label { Layout.fillWidth: true; text: modelData.mode + " · " + modelData.expression; color: theme.secondaryText; wrapMode: Text.WrapAnywhere }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 8
                            ActionButton {
                                objectName: "loadSavedQuery_" + modelData.key
                                text: qsTr("载入查询草稿")
                                property var pressedAction: null
                                onPressed: pressedAction = {"key": modelData.key, "revision": modelData.revision, "generation": panel.controller.generation}
                                onClicked: { if (pressedAction) panel.controller.loadDraft(pressedAction.key, pressedAction.revision, pressedAction.generation); pressedAction = null }
                            }
                            ActionButton {
                                objectName: "renameSavedQuery_" + modelData.key
                                text: qsTr("改名")
                                enabled: panel.controller.canWrite
                                property var pressedAction: null
                                onPressed: pressedAction = {"key": modelData.key, "revision": modelData.revision, "generation": panel.controller.generation, "name": modelData.name}
                                onClicked: { if (pressedAction) { renameDialog.frozenAction = pressedAction; renameInput.text = pressedAction.name; renameDialog.open() }; pressedAction = null }
                            }
                            ActionButton {
                                objectName: "deleteSavedQuery_" + modelData.key
                                text: qsTr("删除")
                                enabled: panel.controller.canWrite
                                property var pressedAction: null
                                onPressed: pressedAction = {"key": modelData.key, "revision": modelData.revision, "generation": panel.controller.generation}
                                onClicked: { if (pressedAction && panel.controller.prepareDelete(pressedAction.key, pressedAction.revision, pressedAction.generation)) deleteDialog.open(); pressedAction = null }
                            }
                        }
                    }
                }
            }
            ActionButton { objectName: "loadMoreSavedQueriesButton"; visible: panel.controller.hasMore; text: qsTr("加载更多"); onClicked: panel.controller.loadMore() }
            ActionButton { objectName: "closeSavedQueryPanelButton"; text: qsTr("关闭"); Layout.alignment: Qt.AlignRight; onClicked: panel.close() }
        }
    }
}
