import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "eagleImportDialog"
    required property var controller
    required property var connection
    required property QtObject theme
    property string postKey: ""
    property int sourceRevision: -1
    readonly property bool currentSource: sourceRevision === controller.sourceRevision
    readonly property bool showResult: postKey === controller.lastPostId
    readonly property string folderPath: {
        const target = connection.target
        if (!target.valid) return ""
        const row = connection.folders.find(entry => entry.key === target.key)
        return row ? row.name : target.name
    }
    signal chooseTarget()
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(550, parent ? parent.width - 32 : 550)
    height: Math.min(520, parent ? parent.height - 32 : 520)
    modal: true
    title: qsTr("导入作品 #%1 到 Eagle").arg(postKey)
    closePolicy: controller.busy ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside

    function showPost(key, revision) {
        if (revision !== controller.sourceRevision || !key.length) return
        postKey = key
        sourceRevision = revision
        duplicates.currentIndex = 0
        general.checked = false
        meta.checked = false
        chinese.checked = false
        queryText.checked = false
        open()
    }

    contentItem: ScrollView {
        id: body
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            width: body.availableWidth
            spacing: 10
            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: dialog.connection.target.valid
                          ? qsTr("%1 / %2").arg(dialog.connection.library.name).arg(dialog.folderPath)
                          : dialog.connection.supported ? qsTr("请选择 Eagle 素材库内的目标文件夹。") : dialog.connection.statusText
                    font.bold: true
                    color: dialog.theme.text
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
                ActionButton {
                    objectName: "chooseEagleImportTarget"
                    text: qsTr("选择目标")
                    enabled: dialog.connection.supported && !dialog.controller.busy
                    onClicked: dialog.chooseTarget()
                }
            }
            Label { text: qsTr("遇到重复作品"); color: dialog.theme.text }
            ComboBox {
                id: duplicates
                objectName: "eagleDuplicatePolicy"
                Layout.fillWidth: true
                enabled: !dialog.controller.busy
                model: [qsTr("跳过并显示已有素材编号"), qsTr("更新已有素材的标签和注释")]
            }
            Label {
                Layout.fillWidth: true
                text: qsTr("画师、角色和作品系列的英文标签会随作品导入。")
                color: dialog.theme.secondaryText
                wrapMode: Text.Wrap
            }
            Flow {
                Layout.fillWidth: true
                enabled: !dialog.controller.busy
                CheckBox { id: general; objectName: "eagleIncludeGeneral"; text: qsTr("普通标签") }
                CheckBox { id: meta; objectName: "eagleIncludeMeta"; text: qsTr("元数据标签") }
                CheckBox { id: chinese; objectName: "eagleIncludeChinese"; text: qsTr("已有中文标签") }
                CheckBox {
                    id: queryText
                    objectName: "eagleIncludeQuery"
                    text: qsTr("查询条件")
                    enabled: dialog.controller.queryAvailable
                }
            }
            Label {
                Layout.fillWidth: true
                visible: !dialog.currentSource
                text: qsTr("详情已切换，请重新打开作品的导入窗口。")
                color: dialog.theme.warning
                wrapMode: Text.Wrap
            }
            Label {
                objectName: "eagleImportStatus"
                Layout.fillWidth: true
                visible: text.length > 0
                text: dialog.showResult ? dialog.controller.statusText : ""
                textFormat: Text.PlainText
                color: dialog.theme.text
                wrapMode: Text.WrapAnywhere
            }
            Label {
                Layout.fillWidth: true
                visible: text.length > 0
                text: dialog.showResult ? dialog.controller.detailsText : ""
                textFormat: Text.PlainText
                color: dialog.theme.secondaryText
                wrapMode: Text.WrapAnywhere
            }
        }
    }
    footer: RowLayout {
        spacing: 8
        Item { Layout.fillWidth: true }
        ActionButton {
            objectName: "closeEagleImport"
            text: dialog.controller.busy ? qsTr("取消导入") : qsTr("关闭")
            enabled: !dialog.controller.cancelling
            onClicked: {
                if (dialog.controller.busy) dialog.controller.cancel()
                else dialog.close()
            }
        }
        ActionButton {
            objectName: "confirmEagleImport"
            Layout.rightMargin: 12
            Layout.bottomMargin: 8
            text: qsTr("确认导入")
            primary: true
            enabled: dialog.currentSource && dialog.connection.target.valid
                     && !dialog.connection.busy && !dialog.controller.busy && !eagleBatch.busy
            property var pressedAction: null
            onPressed: pressedAction = {key: dialog.postKey, source: dialog.sourceRevision,
                                       target: dialog.connection.revision, update: duplicates.currentIndex === 1,
                                       general: general.checked, meta: meta.checked, chinese: chinese.checked,
                                       query: queryText.checked && queryText.enabled}
            onClicked: {
                const action = pressedAction
                pressedAction = null
                if (action) dialog.controller.confirm(action.key, action.source, action.target, action.update,
                                                      action.general, action.meta, action.chinese, action.query)
            }
        }
    }
}
