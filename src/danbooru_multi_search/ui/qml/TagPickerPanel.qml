import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: panel
    required property QtObject theme
    required property var picker
    required property string group
    spacing: 6
    onVisibleChanged: { if (picker) { if (visible) picker.resumeLookup(); else picker.stopLookup() } }

    RowLayout {
        Layout.fillWidth: true
        ActionButton {
            objectName: "pickTagsTab"; Layout.fillWidth: true
            text: qsTr("选择标签"); checked: picker.category !== "favorites"
            onClicked: picker.setCategory(catalog.currentValue || "general")
        }
        ActionButton {
            objectName: "favoriteTagsTab"; Layout.fillWidth: true
            text: qsTr("已收藏标签"); checked: picker.category === "favorites"
            onClicked: picker.setCategory("favorites")
        }
    }
    RowLayout {
        Layout.fillWidth: true
        TextField {
            objectName: "tagPickerFilter"; Layout.fillWidth: true; Layout.minimumWidth: 0
            text: picker.searchText; placeholderText: qsTr("标签或中文名称")
            onTextEdited: picker.setSearchText(text)
        }
        ComboBox {
            id: catalog; objectName: "tagPickerCategory"; visible: picker.category !== "favorites"
            textRole: "label"; valueRole: "key"
            model: [{key:"general",label:qsTr("普通")},{key:"artist",label:qsTr("画师")},
                    {key:"character",label:qsTr("角色")},{key:"copyright",label:qsTr("作品系列")}]
            currentIndex: Math.max(0, model.findIndex(row => row.key === picker.category))
            onActivated: picker.setCategory(currentValue)
        }
    }
    ListView {
        id: choices; objectName: "tagPickerRows"
        Layout.fillWidth: true; Layout.preferredHeight: Math.min(200, Math.max(48, contentHeight))
        clip: true; spacing: 2; model: picker.rows
        ScrollBar.vertical: ScrollBar {}
        delegate: RowLayout {
            id: choice
            required property var modelData
            width: choices.width; spacing: 4
            ItemDelegate {
                objectName: "chooseTag_" + choice.modelData.key
                Layout.fillWidth: true; Layout.minimumWidth: 0
                enabled: picker.simpleEditable
                highlighted: choice.modelData.selected
                implicitHeight: 44
                contentItem: Column {
                    Label { width: parent.width; elide: Text.ElideRight; textFormat: Text.PlainText; color: choice.modelData.selected ? theme.accentText : theme.text; text: (choice.modelData.selected ? "✓  " : "") + choice.modelData.name }
                    Label { width: parent.width; elide: Text.ElideRight; textFormat: Text.PlainText; visible: text.length > 0; color: theme.mutedText; font.pixelSize: 11; text: choice.modelData.chinese }
                }
                property var pressedAction: null
                onPressed: pressedAction = {name: choice.modelData.name, group: panel.group}
                onClicked: if (pressedAction && pressedAction.name === choice.modelData.name) picker.toggleTag(pressedAction.name, pressedAction.group)
            }
            ToolButton {
                objectName: "starTag_" + choice.modelData.key
                text: choice.modelData.favorite ? "★" : "☆"
                Accessible.name: (choice.modelData.favorite ? qsTr("取消收藏 ") : qsTr("收藏 ")) + choice.modelData.name
                property var pressedAction: null
                onPressed: pressedAction = {name: choice.modelData.name, desired: !choice.modelData.favorite}
                onClicked: if (pressedAction && pressedAction.name === choice.modelData.name) picker.setFavorite(pressedAction.name, pressedAction.desired)
            }
        }
        Label {
            anchors.centerIn: parent; visible: !choices.count; color: theme.mutedText
            text: picker.lookupBusy ? qsTr("正在搜索站点标签…")
                                    : picker.category === "favorites" ? qsTr("没有符合条件的收藏标签") : qsTr("没有符合条件的标签")
        }
    }
    ActionButton { visible: picker.hasMore; text: qsTr("更多标签"); flat: true; onClicked: picker.showMore() }
    Label { Layout.fillWidth: true; visible: picker.noticeText.length > 0; text: picker.noticeText; wrapMode: Text.Wrap; color: theme.warning }
    Flow {
        Layout.fillWidth: true; visible: picker.selectedRows.length > 0; spacing: 4
        Repeater {
            model: picker.selectedRows
            ActionButton {
                required property var modelData
                objectName: "selectedTag_" + modelData.key
                width: Math.min(implicitWidth, parent.width); flat: true
                text: (modelData.group === "exclude" ? qsTr("排除 ") : modelData.group === "any" ? qsTr("任一 ") : "") + modelData.name + "  ×"
                Accessible.name: qsTr("移除 ") + modelData.name
                property string pressedName: ""
                onPressed: pressedName = modelData.name
                onClicked: if (pressedName === modelData.name) picker.removeTag(pressedName)
            }
        }
    }
}
