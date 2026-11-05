import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: statusBar
    required property var controller
    required property var sessionController
    property bool active: controller.queryState === "waiting"
                          || controller.queryState === "running"
                          || controller.queryState === "retry_wait"
    readonly property color stateColor: {
        if (controller.queryState === "failed")
            return "#f87171"
        if (controller.queryState === "partial_failed"
                || controller.queryState === "partial_limited"
                || controller.queryState === "partial_cancelled")
            return "#fbbf24"
        if (active)
            return "#60a5fa"
        if (controller.queryState === "complete")
            return "#4ade80"
        return "#a1a1aa"
    }
    readonly property string emptyStateText: {
        if (controller.oldGalleryVisible
                && (controller.queryState === "failed"
                    || controller.queryState === "partial_failed"
                    || controller.queryState === "partial_cancelled"
                    || controller.queryState === "partial_limited"))
            return qsTr("当前查询失败、取消或受限，画廊仍显示上一查询结果")
        if (controller.queryState === "failed")
            return controller.failureErrorText.length > 0
                   ? controller.failureErrorText : qsTr("当前查询失败")
        if (controller.queryState === "partial_cancelled"
                && controller.visibleCount === 0)
            return qsTr("查询已取消，尚未取得可显示结果")
        if (controller.queryState === "partial_limited"
                && controller.visibleCount === 0)
            return qsTr("有限扫描已经停止，尚未取得可显示结果")
        if (controller.queryState === "complete"
                && controller.uniqueCandidates === 0)
            return qsTr("扫描完成，没有取得候选")
        if (controller.uniqueCandidates > 0
                && controller.localMatches === 0)
            return qsTr("已有候选，但没有作品命中表达式")
        if (controller.localMatches > 0
                && controller.visibleCount === 0)
            return qsTr("表达式已有命中，但全部被本地筛选移除")
        return qsTr("尚未取得候选")
    }

    radius: 10
    color: "#1b1f27"
    border.color: "#343944"
    implicitHeight: content.implicitHeight + 20

    ColumnLayout {
        id: content
        anchors.fill: parent
        anchors.margins: 10
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Rectangle {
                width: 9
                height: 9
                radius: 5
                color: statusBar.stateColor
            }

            Label {
                objectName: "queryStatusText"
                Layout.fillWidth: true
                text: controller.statusText
                color: "#f4f4f5"
                font.bold: true
                wrapMode: Text.Wrap
            }

            Label {
                visible: controller.failureBranch >= 0
                text: qsTr("分支 %1 · 第 %2 次请求")
                      .arg(controller.failureBranch + 1)
                      .arg(controller.failurePage)
                color: "#fda4af"
                font.pixelSize: 11
            }

            Button {
                id: cancelButton
                objectName: "cancelButton"
                text: qsTr("取消")
                visible: controller.canCancel
                enabled: controller.canCancel
                onClicked: sessionController.cancelM2Query()
            }
        }

        Rectangle {
            id: progressTrack
            objectName: "queryProgressBar"
            Layout.fillWidth: true
            Layout.preferredHeight: 4
            visible: statusBar.active
            color: "#263244"
            radius: 2
            clip: true

            Rectangle {
                id: progressPulse
                width: Math.max(28, parent.width * 0.32)
                height: parent.height
                radius: 2
                color: "#60a5fa"
                x: 0
                SequentialAnimation on x {
                    running: statusBar.active
                    loops: Animation.Infinite
                    NumberAnimation {
                        from: 0
                        to: Math.max(0, progressTrack.width - progressPulse.width)
                        duration: 900
                        easing.type: Easing.InOutQuad
                    }
                    NumberAnimation {
                        from: Math.max(0, progressTrack.width - progressPulse.width)
                        to: 0
                        duration: 900
                        easing.type: Easing.InOutQuad
                    }
                }
            }
        }

        GridLayout {
            Layout.fillWidth: true
            columns: width >= 660 ? 4 : 2
            columnSpacing: 8
            rowSpacing: 8

            Repeater {
                model: [
                    { "label": qsTr("远程返回"), "value": controller.remoteRows,
                      "objectName": "remoteRowsCount" },
                    { "label": qsTr("已扫描"), "value": controller.uniqueCandidates,
                      "objectName": "uniqueCandidatesCount" },
                    { "label": qsTr("表达式命中"), "value": controller.localMatches,
                      "objectName": "localMatchesCount" },
                    { "label": qsTr("筛选后可见"), "value": controller.visibleCount,
                      "objectName": "visibleCount" }
                ]

                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.preferredHeight: 52
                    radius: 7
                    color: "#252a34"
                    border.color: "#3f4653"

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        Label {
                            Layout.fillWidth: true
                            text: modelData.label
                            color: "#a1a1aa"
                            font.pixelSize: 11
                        }
                        Label {
                            objectName: modelData.objectName
                            text: modelData.value
                            color: "#f4f4f5"
                            font.pixelSize: 20
                            font.bold: true
                        }
                    }
                }
            }
        }

        Label {
            objectName: "oldGalleryNotice"
            Layout.fillWidth: true
            visible: controller.oldGalleryVisible
            text: qsTr("当前查询尚未替换画廊，下面仍显示上一查询结果。")
            color: "#fbbf24"
            wrapMode: Text.Wrap
        }

        Label {
            objectName: "queryFailureText"
            Layout.fillWidth: true
            visible: controller.failureErrorText.length > 0
            text: controller.failureErrorText
                  + (controller.failureHttpStatus > 0
                     ? qsTr("（HTTP %1）").arg(controller.failureHttpStatus)
                     : "")
            color: "#fda4af"
            wrapMode: Text.Wrap
        }
    }
}
