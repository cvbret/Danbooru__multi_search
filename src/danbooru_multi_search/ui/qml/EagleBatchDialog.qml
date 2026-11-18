import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "eagleBatchDialog"
    required property var controller
    required property var connection
    required property QtObject theme
    property bool selectedOnly: false
    property bool submitted: false
    property string sourceToken: ""
    readonly property var progress: controller.progress
    signal chooseTarget()
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(620, parent ? parent.width - 24 : 620)
    height: Math.min(700, parent ? parent.height - 24 : 700)
    modal: true
    closePolicy: controller.busy ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    title: submitted ? qsTr("Eagle 采集进度") : selectedOnly ? qsTr("采集已勾选作品") : qsTr("采集当前查询结果")

    function showScope(token, selected) {
        if (!token || controller.pending.token !== token) return
        sourceToken = token
        selectedOnly = selected
        submitted = false
        mode.currentIndex = 0
        count.value = 20
        position.value = 1
        more.checked = controller.pending.more && !selected
        score.clear()
        favorites.clear()
        quality.currentIndex = fallback.currentIndex = duplicates.currentIndex = 0
        for (const box of [general, meta, chinese, query]) box.checked = false
        for (const group of [types, ratings])
            for (let i = 0; i < group.count; i++) group.itemAt(i).checked = false
        open()
    }
    function checkedValues(group) {
        const values = []
        for (let i = 0; i < group.count; i++)
            if (group.itemAt(i).checked) values.push(group.itemAt(i).modelData.key)
        return values
    }
    function options() {
        return {mode: selectedOnly ? "selected" : mode.currentIndex === 1 ? "from_position" : "first",
                count: count.value, start: position.value, more: more.checked && more.enabled,
                score: score.text.trim(), favorites: favorites.text.trim(),
                types: checkedValues(types), ratings: checkedValues(ratings),
                quality: ["original","large","preview"][quality.currentIndex],
                fallback: quality.currentIndex === 0 ? ["","large","preview"][fallback.currentIndex] : "",
                update: duplicates.currentIndex === 1, general: general.checked, meta: meta.checked,
                chinese: chinese.checked, query: query.checked}
    }
    contentItem: StackLayout {
        currentIndex: dialog.submitted ? 1 : 0
        ScrollView {
            id: form
            objectName: "eagleBatchForm"
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                width: form.availableWidth
                spacing: 9
                Label {
                    Layout.fillWidth: true; color: dialog.theme.text; font.bold: true; wrapMode: Text.Wrap
                    text: dialog.selectedOnly ? qsTr("已勾选 %1 件，按画廊顺序处理。").arg(dialog.controller.pending.count)
                                             : qsTr("当前已加载 %1 件作品。").arg(dialog.controller.pending.count)
                }
                Label {
                    Layout.fillWidth: true; color: dialog.theme.secondaryText; wrapMode: Text.Wrap
                    text: (dialog.controller.pending.partial ? qsTr("当前为部分结果，候选仅来自已扫描范围。") : "")
                          + dialog.controller.pending.range
                }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: qsTr("目标"); color: dialog.theme.text }
                    Label {
                        Layout.fillWidth: true; Layout.minimumWidth: 0; color: dialog.theme.text; wrapMode: Text.WrapAnywhere
                        textFormat: Text.PlainText
                        text: {
                            const connection = dialog.connection
                            if (!connection.supported) return connection.statusText
                            if (!connection.target.valid) return qsTr("请选择目标文件夹")
                            const row = connection.folders.find(item => item.key === connection.target.key)
                            return connection.library.name + " / " + (row ? row.name : connection.target.name)
                        }
                    }
                    ActionButton { objectName: "chooseEagleBatchTarget"; text: qsTr("选择目标"); enabled: dialog.connection.supported; onClicked: dialog.chooseTarget() }
                }
                RowLayout {
                    visible: !dialog.selectedOnly
                    ComboBox { id: mode; objectName: "batchCandidateMode"; model: [qsTr("从第一项开始"), qsTr("指定起始位置")] }
                    SpinBox { id: position; objectName: "batchStartPosition"; visible: mode.currentIndex === 1; from: 1; to: 1000; editable: true; Accessible.name: qsTr("起始位置") }
                    Label { text: qsTr("数量"); color: dialog.theme.text }
                    SpinBox { id: count; objectName: "batchCandidateCount"; from: 1; to: 100; value: 20; editable: true }
                }
                CheckBox { id: more; objectName: "batchAllowMore"; text: qsTr("数量不足时在原查询限额内继续扫描"); visible: !dialog.selectedOnly && dialog.controller.pending.partial; enabled: visible && dialog.controller.pending.more }
                Label { text: qsTr("文件类型（不勾选表示不限）"); color: dialog.theme.text }
                Flow {
                    Layout.fillWidth: true
                    Repeater { id: types; model: [{key:"static",name:qsTr("静态图")},{key:"gif",name:"GIF"},{key:"webm",name:"WebM"},{key:"mp4",name:"MP4"}]; CheckBox { required property var modelData; objectName: "batchType_" + modelData.key; text: modelData.name } }
                }
                Label { text: qsTr("内容分级（不勾选表示不限）"); color: dialog.theme.text }
                Flow {
                    Layout.fillWidth: true
                    Repeater { id: ratings; model: [{key:"g",name:qsTr("一般")},{key:"s",name:qsTr("敏感")},{key:"q",name:qsTr("可疑")},{key:"e",name:qsTr("露骨")}]; CheckBox { required property var modelData; objectName: "batchRating_" + modelData.key; text: modelData.name } }
                }
                RowLayout {
                    Layout.fillWidth: true
                    TextField { id: score; objectName: "batchMinimumScore"; Layout.fillWidth: true; placeholderText: qsTr("最低得分"); validator: IntValidator {} }
                    TextField { id: favorites; objectName: "batchMinimumFavorites"; Layout.fillWidth: true; placeholderText: qsTr("最低收藏数"); validator: IntValidator { bottom: 0 } }
                }
                RowLayout {
                    Label { text: qsTr("清晰度"); color: dialog.theme.text }
                    ComboBox { id: quality; objectName: "batchMediaQuality"; model: [qsTr("原图"),qsTr("较大图片"),qsTr("预览图")] }
                    ComboBox { id: fallback; objectName: "batchMediaFallback"; visible: quality.currentIndex === 0; model: [qsTr("缺原图时记录失败"),qsTr("缺原图时使用较大图片"),qsTr("缺原图时使用预览图")] }
                }
                RowLayout {
                    Label { text: qsTr("重复作品"); color: dialog.theme.text }
                    ComboBox { id: duplicates; objectName: "batchDuplicatePolicy"; model: [qsTr("跳过"),qsTr("更新已有素材的标签和注释")] }
                }
                Label { text: qsTr("附加元数据"); color: dialog.theme.text }
                Flow {
                    Layout.fillWidth: true
                    CheckBox { id: general; text: qsTr("普通标签"); objectName: "batchIncludeGeneral" }
                    CheckBox { id: meta; text: qsTr("元数据标签"); objectName: "batchIncludeMeta" }
                    CheckBox { id: chinese; text: qsTr("已有中文标签"); objectName: "batchIncludeChinese" }
                    CheckBox { id: query; text: qsTr("查询条件"); objectName: "batchIncludeQuery" }
                }
                Label {
                    Layout.fillWidth: true; visible: text.length > 0; color: dialog.theme.error; wrapMode: Text.Wrap
                    text: dialog.controller.errorText || (!dialog.controller.pending.valid ? qsTr("作品来源已改变，请重新打开采集窗口。") : "")
                }
            }
        }
        ColumnLayout {
            Label { text: dialog.progress.label; color: dialog.theme.text; font.bold: true; font.pixelSize: 20 }
            Label {
                Layout.fillWidth: true; color: dialog.theme.text; wrapMode: Text.Wrap
                text: qsTr("计划 %1 件 · 候选 %2 件 · 已处理 %3 件\n成功 %4 件 · 跳过 %5 件 · 失败或待核实 %6 件")
                    .arg(dialog.progress.planned).arg(dialog.progress.count).arg(dialog.progress.processed)
                    .arg(dialog.progress.success).arg(dialog.progress.skipped).arg(dialog.progress.failed)
            }
            Label { Layout.fillWidth: true; color: dialog.theme.secondaryText; wrapMode: Text.Wrap; text: dialog.progress.scope }
            Label { Layout.fillWidth: true; color: dialog.theme.warning; visible: text.length > 0; wrapMode: Text.Wrap; text: dialog.progress.reason }
            Label { color: dialog.theme.text; visible: dialog.progress.current.length > 0; text: qsTr("当前作品 #%1").arg(dialog.progress.current) }
            Label { Layout.fillWidth: true; color: dialog.theme.secondaryText; wrapMode: Text.Wrap; text: qsTr("开始 %1").arg(dialog.progress.started) + (dialog.progress.ended ? qsTr(" · 结束 %1").arg(dialog.progress.ended) : "") }
            ListView {
                id: results; objectName: "batchImportItems"
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 6
                model: dialog.controller.rows
                ScrollBar.vertical: ScrollBar {}
                delegate: Label {
                    required property var modelData
                    width: ListView.view.width - 12
                    wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText; color: dialog.theme.text
                    text: "#" + modelData.key + " · " + (modelData.message || modelData.state)
                        + (modelData.quality ? " · " + modelData.quality + " " + modelData.format.toUpperCase() : "")
                        + (modelData.detail ? "\n" + modelData.detail : "")
                }
            }
        }
    }
    footer: RowLayout {
        Item { Layout.fillWidth: true }
        ActionButton {
            objectName: "closeEagleBatch"; text: dialog.controller.busy ? qsTr("取消采集") : qsTr("关闭")
            enabled: dialog.progress.phase !== "cancelling"
            property string pressedToken: ""
            onPressed: pressedToken = dialog.controller.busy ? dialog.progress.token : ""
            onClicked: {
                if (pressedToken) dialog.controller.cancel(pressedToken)
                else if (!dialog.controller.busy) dialog.close()
                pressedToken = ""
            }
        }
        ActionButton {
            objectName: "confirmEagleBatch"; text: qsTr("开始采集"); primary: true; Layout.rightMargin: 12
            visible: !dialog.submitted
            enabled: dialog.controller.pending.valid && dialog.connection.target.valid && !dialog.connection.busy && !dialog.controller.busy
            property var pressedAction: null
            onPressed: pressedAction = {token: dialog.sourceToken, revision: dialog.connection.revision, options: dialog.options()}
            onClicked: {
                if (pressedAction && dialog.controller.confirm(pressedAction.token, pressedAction.revision, pressedAction.options))
                    dialog.submitted = true
                pressedAction = null
            }
        }
    }
}
