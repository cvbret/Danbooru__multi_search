import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: panel
    property QtObject theme: AppTheme {}
    required property var sessionController
    signal savedQueriesRequested()

    ColumnLayout {
        anchors.fill: parent
        spacing: 16

        ActionButton {
            objectName: "openHistorySavedQueriesButton"
            text: qsTr("常用查询")
            onClicked: panel.savedQueriesRequested()
        }

        RowLayout {
            Layout.fillWidth: true
            Label {
                Layout.fillWidth: true
                text: qsTr("最近查询")
                color: theme.text
                font.pixelSize: 16
                font.bold: true
            }
            ActionButton {
                id: clearHistoryButton
                objectName: "clearHistoryButton"
                text: qsTr("清理历史")
                enabled: historyList.count > 0
                onClicked: sessionController.clearHistory()
            }
        }

        ListView {
            id: historyList
            objectName: "historyList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: theme.smallGap
            model: sessionController.historyModel

            delegate: ItemDelegate {
                id: historyRow
                required property int index
                required property int historyId
                required property string modeText
                required property string expressionText
                required property string ratingText
                required property string stateValue
                required property string stateText
                required property string reasonText
                required property int remoteRows
                required property int uniqueCandidates
                required property int localMatches
                required property int visibleCount
                width: historyList.width
                height: historyContent.implicitHeight + 20
                highlighted: historyId === sessionController.activeHistoryId
                onClicked: sessionController.selectHistory(index)

                background: Rectangle {
                    radius: 8
                    color: historyRow.highlighted || historyRow.down
                           ? theme.selected : historyRow.hovered ? theme.raised : theme.card
                    border.color: historyRow.highlighted ? theme.accent : theme.borderStrong
                }

                contentItem: ColumnLayout {
                    id: historyContent
                    spacing: 5
                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: historyRow.modeText
                            color: theme.accentText
                            font.bold: true
                        }
                        Label {
                            Layout.fillWidth: true
                            text: historyRow.ratingText
                            color: theme.mutedText
                            horizontalAlignment: Text.AlignRight
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: historyRow.expressionText
                        color: theme.text
                        wrapMode: Text.Wrap
                        maximumLineCount: 3
                        elide: Text.ElideRight
                    }
                    Label {
                        Layout.fillWidth: true
                        text: historyRow.stateText
                              + (historyRow.reasonText.length > 0
                                 ? " · " + historyRow.reasonText : "")
                        color: historyRow.stateValue === "failed"
                               ? theme.error
                               : historyRow.stateValue.indexOf("partial_") === 0
                                 ? theme.warning : theme.success
                        wrapMode: Text.Wrap
                    }
                    Label {
                        Layout.fillWidth: true
                        text: qsTr("远程 %1 · 已扫描 %2 · 命中 %3 · 可见 %4")
                              .arg(historyRow.remoteRows)
                              .arg(historyRow.uniqueCandidates)
                              .arg(historyRow.localMatches)
                              .arg(historyRow.visibleCount)
                        color: theme.mutedText
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                }
            }

            Label {
                anchors.centerIn: parent
                visible: historyList.count === 0
                text: qsTr("暂无查询历史")
                color: theme.subtleText
            }
        }

        Label {
            id: sessionNotice
            objectName: "sessionNotice"
            Layout.fillWidth: true
            visible: text.length > 0
            text: sessionController.noticeText
            color: theme.warning
            wrapMode: Text.Wrap
        }

        ActionButton {
            id: restoreQueryButton
            objectName: "restoreQueryButton"
            Layout.fillWidth: true
            text: qsTr("重新执行历史查询")
            enabled: sessionController.canRestoreSelected
            onClicked: sessionController.restoreSelected()
        }
    }
}
