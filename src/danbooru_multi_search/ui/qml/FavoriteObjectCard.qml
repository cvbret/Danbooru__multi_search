import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: card
    required property var rowData
    required property bool selected
    required property int actionRevision
    property QtObject theme: AppTheme {}
    signal selectionRequested(string key, bool desired, int revision)
    color: theme.card
    radius: theme.radius
    border.color: selected ? theme.accent : theme.border
    objectName: "favoriteObject_" + rowData.favoriteKey

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 12
        CheckBox {
            objectName: "favoriteSelect_" + card.rowData.favoriteKey
            text: qsTr("选择此项")
            checked: card.selected
            property var pressedAction: null
            onPressed: pressedAction = { "key": card.rowData.favoriteKey, "revision": card.actionRevision, "desired": !checked }
            onClicked: {
                if (pressedAction)
                    card.selectionRequested(pressedAction.key, pressedAction.desired, pressedAction.revision)
                pressedAction = null
            }
        }
        Label { text: card.rowData.typeText; color: theme.accentText; font.bold: true }
        Label {
            Layout.fillWidth: true
            text: card.rowData.displayName
            color: theme.text
            font.pixelSize: 19
            wrapMode: Text.Wrap
        }
        Label {
            Layout.fillWidth: true
            text: card.rowData.objectKey
            color: theme.secondaryText
            wrapMode: Text.WrapAnywhere
        }
        Item { Layout.fillHeight: true }
        Label {
            Layout.fillWidth: true
            text: card.rowData.objectType === "pool" ? qsTr("已保存的系列作品池信息") : qsTr("已保存的标签")
            color: theme.mutedText
            wrapMode: Text.Wrap
        }
        Label { text: card.rowData.timeText; color: theme.subtleText }
    }
}
