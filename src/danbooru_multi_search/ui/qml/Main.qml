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
        anchors.fill: parent
        anchors.margins: 28
        spacing: 14

        Label {
            Layout.alignment: Qt.AlignHCenter
            text: qsTr("Danbooru Multi Search")
            color: "#f4f4f5"
            font.pixelSize: 28
            font.bold: true
        }

        Label {
            Layout.alignment: Qt.AlignHCenter
            text: qsTr("M0 技术基线 · 单图请求验证")
            color: "#a1a1aa"
            font.pixelSize: 16
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.maximumWidth: 820
            Layout.alignment: Qt.AlignHCenter
            spacing: 10

            TextField {
                id: tagsInput
                Layout.fillWidth: true
                placeholderText: qsTr("输入一个 Danbooru 标签，例如 landscape")
                text: "landscape"
                enabled: !searchController.busy
                selectByMouse: true
                onAccepted: {
                    if (!searchController.busy)
                        searchController.search(text)
                }
            }

            Button {
                text: searchController.busy ? qsTr("请求中……") : qsTr("搜索一张")
                enabled: !searchController.busy
                onClicked: searchController.search(tagsInput.text)
            }
        }

        Label {
            Layout.fillWidth: true
            Layout.maximumWidth: 820
            Layout.alignment: Qt.AlignHCenter
            horizontalAlignment: Text.AlignHCenter
            text: searchController.statusText
            color: searchController.state === "error" ? "#fda4af" : "#d4d4d8"
            wrapMode: Text.Wrap
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 280
            radius: 10
            color: "#0f1115"
            border.color: "#2d3139"

            BusyIndicator {
                anchors.centerIn: parent
                running: searchController.busy
                visible: running
            }

            Image {
                id: resultImage
                anchors.fill: parent
                anchors.margins: 16
                source: searchController.imageUrl
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                cache: true
                visible: source.toString().length > 0
            }

            Label {
                anchors.centerIn: parent
                visible: !searchController.busy
                         && searchController.imageUrl.length === 0
                         && searchController.state !== "error"
                text: searchController.state === "empty"
                      ? qsTr("没有搜索结果")
                      : qsTr("搜索结果将在这里显示")
                color: "#71717a"
            }

            Label {
                anchors.centerIn: parent
                width: Math.min(parent.width - 48, 640)
                visible: searchController.state === "error"
                text: searchController.errorText
                color: "#fda4af"
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
            }

            Label {
                anchors.centerIn: parent
                visible: resultImage.status === Image.Error
                text: qsTr("图片地址已经取得，但图片加载失败。")
                color: "#fda4af"
            }
        }

        Label {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            text: searchController.postSummary
            color: "#a1a1aa"
            elide: Text.ElideRight
            visible: text.length > 0
        }
    }
}
