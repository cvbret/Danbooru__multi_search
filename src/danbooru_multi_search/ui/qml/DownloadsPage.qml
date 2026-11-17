import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Item {
    id: page
    objectName: "downloadsPage"
    required property var controller
    signal poolDownloadsRequested()
    property QtObject theme: AppTheme {}

    FolderDialog {
        id: defaultDirectoryDialog
        objectName: "defaultDownloadFolderDialog"
        property string choiceToken: ""
        title: qsTr("选择默认下载目录")
        currentFolder: page.controller.defaultDirectoryUrl
        function acceptSelection(folder) { page.controller.acceptDirectory(folder, choiceToken) }
        onAccepted: acceptSelection(selectedFolder)
        onRejected: page.controller.cancelDirectorySelection(choiceToken)
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Label { Layout.fillWidth: true; text: qsTr("下载管理"); font.pixelSize: 25; font.bold: true; color: theme.text }
            ActionButton { objectName: "refreshDownloadsButton"; text: qsTr("刷新记录"); onClicked: page.controller.reload() }
            ActionButton { objectName: "showPoolDownloadsButton"; text: qsTr("整池下载准备"); onClicked: page.poolDownloadsRequested() }
        }
        Label { Layout.fillWidth: true; text: qsTr("未完成任务需要手动从头恢复。切换页面不会取消已经确认的下载。"); color: theme.secondaryText; wrapMode: Text.Wrap }
        Flow {
            Layout.fillWidth: true
            spacing: 8
            ActionButton {
                objectName: "chooseDefaultDownloadDirectoryButton"
                text: qsTr("修改默认目录")
                onClicked: {
                    defaultDirectoryDialog.choiceToken = page.controller.beginDirectorySelection("default")
                    if (defaultDirectoryDialog.choiceToken.length) defaultDirectoryDialog.open()
                }
            }
            Label { width: Math.max(200, page.width - 200); text: page.controller.defaultDirectory; color: theme.secondaryText; wrapMode: Text.WrapAnywhere }
        }
        Label { objectName: "downloadsError"; Layout.fillWidth: true; visible: text.length > 0; text: page.controller.errorText; color: theme.error; wrapMode: Text.Wrap }
        Label { Layout.fillWidth: true; visible: page.controller.noticeText.length > 0; text: page.controller.noticeText; color: theme.secondaryText; wrapMode: Text.Wrap }
        ToolButton { id: errorDetailsToggle; visible: page.controller.errorDetails.length > 0; text: checked ? qsTr("收起详细原因") : qsTr("查看详细原因"); checkable: true }
        Label { Layout.fillWidth: true; visible: errorDetailsToggle.visible && errorDetailsToggle.checked; text: page.controller.errorDetails; color: theme.mutedText; wrapMode: Text.WrapAnywhere; maximumLineCount: 4; elide: Text.ElideRight }

        GridLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: page.width >= 1080 ? 2 : 1
            rowSpacing: 12
            columnSpacing: 16
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: page.width >= 1080
                Layout.preferredWidth: 320
                Layout.preferredHeight: page.width >= 1080 ? 400 : 140
                spacing: 6
                Label { text: qsTr("下载批次"); font.bold: true; color: theme.text }
                ListView {
                    id: batchList
                    objectName: "downloadBatchList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 6
                    model: page.controller.batchesModel
                    ScrollBar.vertical: ScrollBar {}
                    delegate: ItemDelegate {
                        required property string batchKey
                        required property string titleText
                        required property string summaryText
                        required property string progressText
                        objectName: "downloadBatch_" + batchKey
                        width: ListView.view.width
                        implicitHeight: 94
                        background: Rectangle { color: page.controller.selectedBatchKey === batchKey ? theme.selected : theme.surface; border.color: theme.border; radius: theme.smallRadius }
                        contentItem: ColumnLayout {
                            Label { Layout.fillWidth: true; text: titleText; color: theme.text; font.bold: true; elide: Text.ElideRight }
                            Label { Layout.fillWidth: true; text: summaryText; color: theme.secondaryText; elide: Text.ElideRight }
                            Label { Layout.fillWidth: true; text: progressText; color: theme.mutedText; elide: Text.ElideRight }
                        }
                        property string pressedKey: ""
                        onPressed: pressedKey = batchKey
                        onClicked: page.controller.selectBatch(pressedKey)
                    }
                }
                ActionButton { objectName: "loadMoreDownloadBatchesButton"; text: qsTr("加载更多批次"); visible: page.controller.canLoadMore; onClicked: page.controller.loadMoreBatches() }
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 700
                spacing: 8
                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    Label { height: 40; verticalAlignment: Text.AlignVCenter; text: qsTr("本批次 %1 项").arg(page.controller.itemsModel.count); font.bold: true; color: theme.text }
                    ActionButton {
                        objectName: "cancelDownloadBatchButton"
                        text: qsTr("取消未完成项")
                        enabled: page.controller.canCancelBatch
                        property string pressedToken: ""
                        onPressed: pressedToken = page.controller.captureBatchAction(page.controller.selectedBatchKey, "cancel")
                        onClicked: { page.controller.cancelBatch(pressedToken); pressedToken = "" }
                    }
                    ActionButton {
                        objectName: "resumeDownloadBatchButton"
                        text: qsTr("从头恢复本批次")
                        enabled: page.controller.canResumeBatch
                        property string pressedToken: ""
                        onPressed: pressedToken = page.controller.captureBatchAction(page.controller.selectedBatchKey, "resume")
                        onClicked: { page.controller.resumeBatch(pressedToken); pressedToken = "" }
                    }
                }
                ListView {
                    objectName: "downloadItemList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: 90
                    model: page.controller.itemsModel
                    spacing: 8
                    clip: true
                    ScrollBar.vertical: ScrollBar {}
                    delegate: Rectangle {
                        id: itemRow
                        required property string itemKey
                        required property string generationKey
                        required property string postKey
                        required property string extensionText
                        required property string fileName
                        required property string targetText
                        required property string stateText
                        required property string stateCode
                        required property string progressText
                        required property real progressValue
                        required property bool indeterminate
                        required property string attemptsText
                        required property string reasonText
                        required property string detailText
                        required property bool canCancel
                        required property bool canRetry
                        width: ListView.view.width
                        implicitHeight: content.implicitHeight + 24
                        color: theme.surface
                        border.color: theme.border
                        radius: theme.smallRadius
                        ColumnLayout {
                            id: content
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 12
                            spacing: 8
                            Label { Layout.fillWidth: true; text: qsTr("作品 #%1 · %2 · %3").arg(itemRow.postKey).arg(itemRow.extensionText).arg(itemRow.stateText); font.bold: true; color: itemRow.stateCode === "failed" ? theme.error : theme.text; wrapMode: Text.Wrap }
                            Label { Layout.fillWidth: true; text: itemRow.targetText; color: theme.secondaryText; wrapMode: Text.WrapAnywhere }
                            ProgressBar { Layout.fillWidth: true; value: itemRow.progressValue; indeterminate: itemRow.indeterminate }
                            Label { Layout.fillWidth: true; text: itemRow.progressText + " · " + itemRow.attemptsText; color: theme.mutedText; wrapMode: Text.Wrap }
                            Label { Layout.fillWidth: true; visible: itemRow.reasonText.length > 0; text: itemRow.reasonText; color: itemRow.stateCode === "failed" ? theme.error : theme.secondaryText; wrapMode: Text.Wrap }
                            Flow {
                                Layout.fillWidth: true
                                spacing: 8
                                ActionButton {
                                    objectName: "cancelDownloadItem_" + itemRow.itemKey
                                    text: qsTr("取消下载")
                                    visible: itemRow.canCancel
                                    property var pressedAction: null
                                    onPressed: pressedAction = {"key": itemRow.itemKey, "generation": itemRow.generationKey}
                                    onClicked: { if (pressedAction) page.controller.cancelItem(pressedAction.key, pressedAction.generation); pressedAction = null }
                                }
                                ActionButton {
                                    objectName: "retryDownloadItem_" + itemRow.itemKey
                                    text: itemRow.stateCode === "awaiting_resume" ? qsTr("从头恢复此项") : qsTr("重新下载此项")
                                    visible: itemRow.canRetry
                                    property var pressedAction: null
                                    onPressed: pressedAction = {"key": itemRow.itemKey, "generation": itemRow.generationKey}
                                    onClicked: { if (pressedAction) page.controller.retryItem(pressedAction.key, pressedAction.generation); pressedAction = null }
                                }
                                ToolButton { id: itemDetails; visible: itemRow.detailText.length > 0; text: checked ? qsTr("收起原因") : qsTr("详细原因"); checkable: true }
                            }
                            Label { Layout.fillWidth: true; visible: itemDetails.checked && itemDetails.visible; text: itemRow.detailText; color: theme.mutedText; wrapMode: Text.WrapAnywhere }
                        }
                    }
                }
            }
        }
        Label { objectName: "downloadsEmptyState"; Layout.fillWidth: true; visible: page.controller.batchesModel.count === 0; text: qsTr("还没有下载任务。在作品卡片、画廊已选作品或详情中选择“下载原文件”开始。"); color: theme.secondaryText; wrapMode: Text.Wrap }
    }
}
