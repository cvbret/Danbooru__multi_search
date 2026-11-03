import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    visible: true
    width: 1120
    height: 720
    minimumWidth: 760
    minimumHeight: 520
    title: qsTr("Danbooru Multi Search")
    color: "#15171c"

    ColumnLayout {
        anchors.centerIn: parent
        spacing: 12

        Label {
            Layout.alignment: Qt.AlignHCenter
            text: qsTr("Danbooru Multi Search")
            color: "#f4f4f5"
            font.pixelSize: 28
            font.bold: true
        }

        Label {
            Layout.alignment: Qt.AlignHCenter
            text: qsTr("M0 技术基线")
            color: "#a1a1aa"
            font.pixelSize: 16
        }
    }
}

