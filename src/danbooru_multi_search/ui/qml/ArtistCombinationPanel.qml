import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: panel
    objectName: "artistCombinationPanel"
    required property var controller
    property QtObject theme: AppTheme {}
    property var insertionPath: null
    signal dismissed()
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(720, parent ? parent.width - 32 : 720)
    height: Math.min(740, parent ? parent.height - 32 : 740)
    modal: true
    title: qsTr("收藏画师与常用组合")
    onAboutToShow: controller.setActive(true)
    onClosed: {
        renameDialog.close()
        deleteDialog.close()
        controller.setActive(false)
        dismissed()
    }

    Dialog {
        id: renameDialog
        objectName: "renameArtistCombinationDialog"
        property string frozenKey: ""
        property int frozenGeneration: -1
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, parent ? parent.width - 32 : 440)
        modal: true
        title: qsTr("修改组合名称")
        contentItem: ColumnLayout {
            spacing: 12
            TextField { id: renameInput; objectName: "artistCombinationRenameInput"; Layout.fillWidth: true; placeholderText: qsTr("组合名称") }
            Label { Layout.fillWidth: true; text: panel.controller.errorText; visible: text.length > 0; color: theme.error; wrapMode: Text.Wrap }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ActionButton { text: qsTr("取消"); onClicked: renameDialog.close() }
                ActionButton {
                    objectName: "confirmArtistCombinationRenameButton"
                    text: qsTr("保存名称")
                    enabled: panel.controller.canWrite && renameInput.text.trim().length > 0
                    onClicked: {
                        if (panel.controller.renameCombination(renameDialog.frozenKey, renameDialog.frozenGeneration, renameInput.text))
                            renameDialog.close()
                    }
                }
            }
        }
    }

    Dialog {
        id: deleteDialog
        objectName: "deleteArtistCombinationDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, parent ? parent.width - 32 : 440)
        modal: true
        title: qsTr("删除常用组合")
        onRejected: panel.controller.cancelDelete()
        contentItem: ColumnLayout {
            spacing: 16
            Label {
                Layout.fillWidth: true
                text: qsTr("删除组合“%1”？画师收藏和查询草稿保持不变。").arg(panel.controller.deleteConfirmation.name)
                color: theme.text
                wrapMode: Text.Wrap
            }
            Label { Layout.fillWidth: true; text: panel.controller.errorText; visible: text.length > 0; color: theme.error; wrapMode: Text.Wrap }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ActionButton { objectName: "cancelArtistCombinationDeleteButton"; text: qsTr("取消"); onClicked: { panel.controller.cancelDelete(); deleteDialog.close() } }
                ActionButton {
                    objectName: "confirmArtistCombinationDeleteButton"
                    text: qsTr("确认删除组合")
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
        objectName: "artistCombinationScroll"
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: parent.width
            spacing: 16
            Label {
                Layout.fillWidth: true
                text: qsTr("只把条件加入查询草稿，不会自动搜索。保存的组合独立保留，取消画师收藏不会删除组合；收藏 JSON 不包含组合。")
                color: theme.secondaryText
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Label { text: qsTr("从已收藏画师选择"); font.bold: true; color: theme.text; Layout.fillWidth: true }
                ActionButton { objectName: "reloadArtistCombinationsButton"; text: qsTr("刷新"); onClicked: panel.controller.reload() }
            }
            Label { Layout.fillWidth: true; visible: panel.controller.artists.length === 0; text: qsTr("还没有收藏画师。可以在作品详情的画师标签菜单中选择“本地收藏”。"); color: theme.mutedText; wrapMode: Text.Wrap }
            ListView {
                objectName: "savedArtistChoices"
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(contentHeight, panel.height < 650 ? 136 : 200)
                clip: true
                model: panel.controller.artists
                ScrollBar.vertical: ScrollBar {}
                delegate: CheckBox {
                    required property var modelData
                    width: ListView.view.width
                    objectName: "savedArtistChoice_" + modelData.key
                    text: modelData.name
                    checked: modelData.selected
                    property string pressedKey: ""
                    property int pressedGeneration: -1
                    onPressed: { pressedKey = modelData.key; pressedGeneration = panel.controller.generation }
                    onClicked: panel.controller.toggleArtist(pressedKey, pressedGeneration, checked)
                    contentItem: Label { leftPadding: 32; text: parent.text; color: theme.text; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                }
            }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ComboBox {
                    id: selectionMode
                    objectName: "savedArtistMode"
                    width: Math.min(180, panel.width - 56)
                    textRole: "label"; valueRole: "value"
                    model: [{"value":"all","label":qsTr("全部包含")},{"value":"any","label":qsTr("任意包含")},{"value":"exclude","label":qsTr("排除") }]
                }
                ActionButton {
                    objectName: "applySavedArtistSelectionButton"
                    text: qsTr("加入查询草稿（%1）").arg(panel.controller.selectedCount)
                    enabled: panel.controller.selectedCount > 0
                    property var pressedAction: null
                    onPressed: pressedAction = { "generation": panel.controller.generation, "revision": panel.controller.selectionRevision, "mode": selectionMode.currentValue }
                    onClicked: { if (pressedAction) panel.controller.applySelection(pressedAction.generation, pressedAction.revision, pressedAction.mode, panel.insertionPath); pressedAction = null }
                }
                ActionButton { objectName: "clearSavedArtistSelectionButton"; text: qsTr("清除选择"); enabled: panel.controller.selectedCount > 0; onClicked: panel.controller.clearSelection() }
            }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                TextField { id: newName; objectName: "artistCombinationNameInput"; width: Math.min(280, panel.width - 56); placeholderText: qsTr("为所选画师填写组合名称") }
                ActionButton {
                    objectName: "saveArtistCombinationButton"
                    text: qsTr("保存为常用组合")
                    enabled: panel.controller.canWrite && panel.controller.selectedCount > 0 && newName.text.trim().length > 0
                    property var pressedAction: null
                    onPressed: pressedAction = { "generation": panel.controller.generation, "revision": panel.controller.selectionRevision, "mode": selectionMode.currentValue, "name": newName.text }
                    onClicked: {
                        if (pressedAction && panel.controller.saveSelection(pressedAction.generation, pressedAction.revision, pressedAction.name, pressedAction.mode))
                            newName.clear()
                        pressedAction = null
                    }
                }
            }
            Label { objectName: "artistCombinationError"; Layout.fillWidth: true; visible: text.length > 0; text: panel.controller.errorText; color: theme.error; wrapMode: Text.Wrap }
            Label { text: qsTr("常用画师组合"); font.bold: true; font.pixelSize: 18; color: theme.text }
            Label { Layout.fillWidth: true; visible: panel.controller.combinations.length === 0; text: qsTr("选择上方画师和模式，然后保存常用组合。"); color: theme.mutedText; wrapMode: Text.Wrap }
            Repeater {
                model: panel.controller.combinations
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: savedContent.implicitHeight + 24
                    radius: theme.smallRadius
                    color: theme.surface
                    border.color: theme.border
                    ColumnLayout {
                        id: savedContent
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 8
                        Label { Layout.fillWidth: true; text: modelData.name; font.bold: true; color: theme.text; wrapMode: Text.Wrap }
                        Label { Layout.fillWidth: true; text: modelData.modeText + " · " + modelData.artists.join("、"); color: theme.secondaryText; wrapMode: Text.WrapAnywhere }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 8
                            ActionButton {
                                objectName: "applyArtistCombination_" + modelData.key
                                text: qsTr("加入查询草稿")
                                property var pressedAction: null
                                onPressed: pressedAction = { "key": modelData.key, "generation": panel.controller.generation }
                                onClicked: { if (pressedAction) panel.controller.applyCombination(pressedAction.key, pressedAction.generation, panel.insertionPath); pressedAction = null }
                            }
                            ActionButton {
                                objectName: "renameArtistCombination_" + modelData.key
                                text: qsTr("改名")
                                enabled: panel.controller.canWrite
                                property var pressedAction: null
                                onPressed: pressedAction = { "key": modelData.key, "generation": panel.controller.generation, "name": modelData.name }
                                onClicked: {
                                    if (!pressedAction) return
                                    renameDialog.frozenKey = pressedAction.key
                                    renameDialog.frozenGeneration = pressedAction.generation
                                    renameInput.text = pressedAction.name
                                    renameDialog.open()
                                    pressedAction = null
                                }
                            }
                            ActionButton {
                                objectName: "deleteArtistCombination_" + modelData.key
                                text: qsTr("删除组合")
                                enabled: panel.controller.canWrite
                                property var pressedAction: null
                                onPressed: pressedAction = { "key": modelData.key, "generation": panel.controller.generation }
                                onClicked: { if (pressedAction && panel.controller.prepareDeleteCombination(pressedAction.key, pressedAction.generation)) deleteDialog.open(); pressedAction = null }
                            }
                        }
                    }
                }
            }
            ActionButton { objectName: "closeArtistCombinationPanelButton"; text: qsTr("关闭"); Layout.alignment: Qt.AlignRight; onClicked: panel.close() }
        }
    }
}
