import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: panel
    objectName: "poolDownloadPanel"
    required property var controller
    required property var downloads
    signal batchRequested(string key)
    property QtObject theme: AppTheme {}
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(760, parent ? parent.width - 24 : 760)
    height: Math.min(740, parent ? parent.height - 24 : 740)
    modal: true
    title: qsTr("整池下载准备")
    onOpened: controller.setActive(true)
    onClosed: controller.setActive(false)
    function showRecords() { open() }
    contentItem: ColumnLayout {
        spacing: 6
        Label {
            Layout.fillWidth: true
            text: qsTr("准备完成表示已加入下载任务。文件状态请查看关联批次。停止准备后，已加入的文件下载继续。")
            color: theme.secondaryText; wrapMode: Text.Wrap
        }
        ListView {
            objectName: "poolDownloadPlanList"
            Layout.fillWidth: true; Layout.preferredHeight: 90; Layout.minimumHeight: 60
            clip: true; model: panel.controller.plansModel; spacing: 4
            ScrollBar.vertical: ScrollBar {}
            delegate: ItemDelegate {
                required property string planKey
                required property string generationKey
                required property string titleText
                required property string summaryText
                width: ListView.view.width; implicitHeight: 72
                objectName: "poolDownloadPlan_" + planKey
                property string pressedKey: ""
                onPressed: pressedKey = planKey
                onClicked: panel.controller.selectPlan(pressedKey)
                contentItem: ColumnLayout {
                    Label { Layout.fillWidth: true; text: titleText; color: theme.text; elide: Text.ElideRight }
                    Label { Layout.fillWidth: true; text: summaryText; color: theme.secondaryText; wrapMode: Text.Wrap }
                }
                background: Rectangle { color: panel.controller.selected.key === planKey ? theme.selected : theme.surface; border.color: theme.border; radius: theme.smallRadius }
            }
        }
        Flow {
            Layout.fillWidth: true; spacing: 8
            ActionButton { text: qsTr("刷新记录"); onClicked: panel.controller.reload() }
            ActionButton { text: qsTr("更多记录"); visible: panel.controller.canLoadMore; onClicked: panel.controller.loadMore() }
            ActionButton { text: qsTr("关闭"); objectName: "closePoolDownloadPanelButton"; onClicked: panel.close() }
        }
        Label { Layout.fillWidth: true; visible: text.length > 0; text: panel.controller.errorText; color: theme.error; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight }
        Label { Layout.fillWidth: true; visible: panel.controller.plansModel.count === 0; text: qsTr("尚无整池计划。请从系列作品池或阅读器选择“下载整个作品池”。"); wrapMode: Text.Wrap; color: theme.secondaryText }
        Label { Layout.fillWidth: true; visible: text.length > 0; text: panel.controller.selected.message || ""; wrapMode: Text.Wrap; color: theme.secondaryText; maximumLineCount: 2; elide: Text.ElideRight }
        Flow {
            Layout.fillWidth: true; spacing: 6
            Repeater {
                model: [{"action":"stop","label":qsTr("停止准备")}, {"action":"resume","label":qsTr("继续未完成准备")}, {"action":"retry","label":qsTr("重试未获得原文件页面")}]
                delegate: ActionButton {
                    required property var modelData
                    objectName: "poolDownloadAction_" + modelData.action
                    text: modelData.label
                    visible: panel.controller.selected.key.length > 0
                    enabled: modelData.action === "stop" ? panel.controller.selected.preparing : !panel.controller.selected.preparing
                    property string pressedToken: ""
                    onPressed: pressedToken = panel.controller.captureAction(panel.controller.selected.key, panel.controller.selected.generation, modelData.action)
                    onClicked: panel.controller.runAction(pressedToken)
                }
            }
        }
        Flow {
            Layout.fillWidth: true; spacing: 6
            ActionButton { text: qsTr("上一组页面"); enabled: panel.controller.selected.hasPrevious || false; onClicked: panel.controller.turnPages(-1) }
            Label { text: qsTr("原始页面 %1 / %2").arg(panel.controller.selected.firstPage).arg(panel.controller.selected.total); height: 40; verticalAlignment: Text.AlignVCenter; color: theme.secondaryText }
            ActionButton { text: qsTr("下一组页面"); enabled: panel.controller.selected.hasNext || false; onClicked: panel.controller.turnPages(1) }
        }
        ListView {
            objectName: "poolDownloadPageList"
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 48
            clip: true; model: panel.controller.pagesModel; spacing: 4
            ScrollBar.vertical: ScrollBar {}
            delegate: Column {
                required property string titleText
                required property string reasonText
                width: ListView.view.width; spacing: 2
                Label { width: parent.width; text: titleText; wrapMode: Text.Wrap; color: theme.text }
                Label { width: parent.width; text: reasonText; visible: text.length > 0; wrapMode: Text.Wrap; color: theme.secondaryText }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            ActionButton { text: qsTr("上一组"); enabled: panel.controller.batchOffset > 0; onClicked: panel.controller.turnBatches(-1) }
            ComboBox { id: batchChoice; Layout.fillWidth: true; model: panel.controller.batchKeys; displayText: count ? qsTr("批次 %1").arg(currentText.slice(0, 8)) : qsTr("尚无关联批次") }
            ActionButton {
                objectName: "viewPoolDownloadBatchButton"
                text: qsTr("查看批次"); enabled: batchChoice.count > 0
                property string pressedKey: ""
                onPressed: pressedKey = batchChoice.currentText
                onClicked: { panel.batchRequested(pressedKey); panel.close() }
            }
            ActionButton { text: qsTr("下一组"); enabled: panel.controller.batchKeys.length === 10; onClicked: panel.controller.turnBatches(1) }
        }
    }
}
