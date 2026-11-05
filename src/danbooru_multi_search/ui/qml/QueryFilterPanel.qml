import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: panel
    required property var controller
    property var formValues: ({})
    property int formRevision: 0
    property bool syncing: false

    function copyForm() {
        return JSON.parse(JSON.stringify(formValues || {}))
    }

    function value(key, fallbackValue) {
        const revision = formRevision
        if (formValues && formValues[key] !== undefined)
            return formValues[key]
        return fallbackValue
    }

    function textValue(key) {
        const current = value(key, "")
        return current === undefined || current === null ? "" : String(current)
    }

    function containsValue(key, requested) {
        const values = value(key, [])
        return Array.isArray(values) && values.indexOf(requested) >= 0
    }

    function setValue(key, newValue) {
        if (syncing)
            return
        const next = copyForm()
        next[key] = newValue
        formValues = next
        ++formRevision
        controller.updateDraftForm(next)
    }

    function toggleListValue(key, requested, checked) {
        const next = []
        const current = value(key, [])
        for (let index = 0; index < current.length; ++index) {
            if (current[index] !== requested)
                next.push(current[index])
        }
        if (checked)
            next.push(requested)
        setValue(key, next)
    }

    function indexFor(model, target) {
        for (let index = 0; index < model.length; ++index) {
            if (model[index].value === target)
                return index
        }
        return 0
    }

    function syncFromController() {
        syncing = true
        formValues = controller.draftFormValues || ({})
        ++formRevision
        syncing = false
    }

    Component.onCompleted: syncFromController()

    Connections {
        target: panel.controller
        function onDraftChanged() {
            panel.syncFromController()
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: 14

            Label {
                text: qsTr("内容与分级")
                color: "#f4f4f5"
                font.pixelSize: 16
                font.bold: true
            }

            ComboBox {
                id: ratingFilter
                objectName: "ratingFilter"
                Layout.fillWidth: true
                textRole: "label"
                valueRole: "value"
                model: [
                    { "label": qsTr("全部分级"), "value": "all" },
                    { "label": "General", "value": "general" },
                    { "label": "Sensitive", "value": "sensitive" },
                    { "label": "Questionable", "value": "questionable" },
                    { "label": "Explicit", "value": "explicit" }
                ]
                currentIndex: panel.indexFor(model, panel.textValue("rating"))
                onActivated: panel.setValue("rating", currentValue)
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: 8
                rowSpacing: 8

                Label { text: qsTr("最低得分"); color: "#a1a1aa" }
                TextField {
                    objectName: "minimumScoreInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("整数，可留空")
                    text: panel.textValue("minimumScore")
                    onTextEdited: panel.setValue("minimumScore", text)
                }

                Label { text: qsTr("最低收藏") ; color: "#a1a1aa" }
                TextField {
                    objectName: "minimumFavCountInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("非负整数")
                    text: panel.textValue("minimumFavCount")
                    onTextEdited: panel.setValue("minimumFavCount", text)
                }

                Label { text: qsTr("发布时间起") ; color: "#a1a1aa" }
                TextField {
                    objectName: "publishedFromInput"
                    Layout.fillWidth: true
                    placeholderText: "YYYY-MM-DD HH:mm"
                    text: panel.textValue("publishedFrom")
                    onTextEdited: panel.setValue("publishedFrom", text)
                }

                Label { text: qsTr("发布时间止") ; color: "#a1a1aa" }
                TextField {
                    objectName: "publishedUntilInput"
                    Layout.fillWidth: true
                    placeholderText: "YYYY-MM-DD HH:mm"
                    text: panel.textValue("publishedUntil")
                    onTextEdited: panel.setValue("publishedUntil", text)
                }

                Label { text: qsTr("最小年龄（天）") ; color: "#a1a1aa" }
                TextField {
                    objectName: "minimumAgeDaysInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("非负整数")
                    text: panel.textValue("minimumAgeDays")
                    onTextEdited: panel.setValue("minimumAgeDays", text)
                }

                Label { text: qsTr("最大年龄（天）") ; color: "#a1a1aa" }
                TextField {
                    objectName: "maximumAgeDaysInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("非负整数")
                    text: panel.textValue("maximumAgeDays")
                    onTextEdited: panel.setValue("maximumAgeDays", text)
                }
            }

            Label {
                text: qsTr("文件类别")
                color: "#f4f4f5"
                font.bold: true
            }

            Flow {
                Layout.fillWidth: true
                spacing: 8
                CheckBox {
                    objectName: "staticFileFilter"
                    text: qsTr("静态图片")
                    checked: panel.containsValue("fileCategories", "static")
                    onToggled: panel.toggleListValue("fileCategories", "static", checked)
                }
                CheckBox {
                    objectName: "gifFileFilter"
                    text: "GIF"
                    checked: panel.containsValue("fileCategories", "gif")
                    onToggled: panel.toggleListValue("fileCategories", "gif", checked)
                }
                CheckBox {
                    objectName: "webmFileFilter"
                    text: "WebM"
                    checked: panel.containsValue("fileCategories", "webm")
                    onToggled: panel.toggleListValue("fileCategories", "webm", checked)
                }
                CheckBox {
                    objectName: "mp4FileFilter"
                    text: "MP4"
                    checked: panel.containsValue("fileCategories", "mp4")
                    onToggled: panel.toggleListValue("fileCategories", "mp4", checked)
                }
            }

            Label {
                text: qsTr("尺寸与长宽比")
                color: "#f4f4f5"
                font.pixelSize: 16
                font.bold: true
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: 8
                rowSpacing: 8

                Label { text: qsTr("最低宽度") ; color: "#a1a1aa" }
                TextField {
                    objectName: "minimumWidthInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("正整数")
                    text: panel.textValue("minimumWidth")
                    onTextEdited: panel.setValue("minimumWidth", text)
                }

                Label { text: qsTr("最低高度") ; color: "#a1a1aa" }
                TextField {
                    objectName: "minimumHeightInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("正整数")
                    text: panel.textValue("minimumHeight")
                    onTextEdited: panel.setValue("minimumHeight", text)
                }

                Label { text: qsTr("最小长宽比") ; color: "#a1a1aa" }
                TextField {
                    objectName: "minimumAspectRatioInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("例如 4:3")
                    text: panel.textValue("minimumAspectRatio")
                    onTextEdited: panel.setValue("minimumAspectRatio", text)
                }

                Label { text: qsTr("最大长宽比") ; color: "#a1a1aa" }
                TextField {
                    objectName: "maximumAspectRatioInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("例如 16:9")
                    text: panel.textValue("maximumAspectRatio")
                    onTextEdited: panel.setValue("maximumAspectRatio", text)
                }

                Label { text: qsTr("精确比例") ; color: "#a1a1aa" }
                ComboBox {
                    objectName: "aspectRatioPreset"
                    Layout.fillWidth: true
                    model: [qsTr("自定义"), "1:1", "4:3", "3:2", "16:9"]
                    onActivated: {
                        if (currentIndex === 0)
                            return
                        const next = panel.copyForm()
                        next.minimumAspectRatio = currentText
                        next.maximumAspectRatio = currentText
                        panel.formValues = next
                        ++panel.formRevision
                        panel.controller.updateDraftForm(next)
                    }
                }
            }

            Label {
                text: qsTr("图片方向")
                color: "#f4f4f5"
                font.bold: true
            }

            Flow {
                Layout.fillWidth: true
                spacing: 8
                CheckBox {
                    objectName: "landscapeFilter"
                    text: qsTr("横向")
                    checked: panel.containsValue("orientations", "landscape")
                    onToggled: panel.toggleListValue("orientations", "landscape", checked)
                }
                CheckBox {
                    objectName: "portraitFilter"
                    text: qsTr("纵向")
                    checked: panel.containsValue("orientations", "portrait")
                    onToggled: panel.toggleListValue("orientations", "portrait", checked)
                }
                CheckBox {
                    objectName: "squareFilter"
                    text: qsTr("正方形")
                    checked: panel.containsValue("orientations", "square")
                    onToggled: panel.toggleListValue("orientations", "square", checked)
                }
            }

            Label {
                text: qsTr("屏蔽标签")
                color: "#f4f4f5"
                font.bold: true
            }

            TextArea {
                objectName: "blockedTagsInput"
                Layout.fillWidth: true
                Layout.preferredHeight: 72
                placeholderText: qsTr("使用逗号或换行分隔")
                wrapMode: TextEdit.Wrap
                text: panel.textValue("blockedTags")
                onTextChanged: {
                    if (activeFocus && !panel.syncing)
                        panel.setValue("blockedTags", text)
                }
            }

            Label {
                text: qsTr("排序")
                color: "#f4f4f5"
                font.pixelSize: 16
                font.bold: true
            }

            RowLayout {
                Layout.fillWidth: true
                ComboBox {
                    id: sortFieldInput
                    objectName: "sortFieldInput"
                    Layout.fillWidth: true
                    textRole: "label"
                    valueRole: "value"
                    model: [
                        { "label": qsTr("来源顺序"), "value": "source_order" },
                        { "label": qsTr("得分"), "value": "score" },
                        { "label": qsTr("收藏数量"), "value": "fav_count" },
                        { "label": qsTr("发布时间"), "value": "created_at" },
                        { "label": qsTr("作品编号"), "value": "post_id" },
                        { "label": qsTr("当前热门顺序"), "value": "rank" }
                    ]
                    currentIndex: panel.indexFor(model, panel.textValue("sortField"))
                    onActivated: {
                        const next = panel.copyForm()
                        next.sortField = currentValue
                        if (currentValue === "source_order" || currentValue === "rank")
                            next.sortDirection = "original"
                        else if (next.sortDirection === "original")
                            next.sortDirection = "descending"
                        panel.formValues = next
                        ++panel.formRevision
                        panel.controller.updateDraftForm(next)
                    }
                }

                ComboBox {
                    id: sortDirectionInput
                    objectName: "sortDirectionInput"
                    Layout.preferredWidth: 112
                    textRole: "label"
                    valueRole: "value"
                    readonly property bool originalOnly:
                        sortFieldInput.currentValue === "source_order"
                        || sortFieldInput.currentValue === "rank"
                    model: originalOnly
                           ? [
                               { "label": qsTr("原顺序"),
                                 "value": "original" }
                             ]
                           : [
                               { "label": qsTr("升序"),
                                 "value": "ascending" },
                               { "label": qsTr("降序"),
                                 "value": "descending" }
                             ]
                    currentIndex: panel.indexFor(model, panel.textValue("sortDirection"))
                    enabled: !originalOnly
                    onActivated: panel.setValue("sortDirection", currentValue)
                }
            }

            Label {
                text: qsTr("执行限制")
                color: "#f4f4f5"
                font.pixelSize: 16
                font.bold: true
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: 8
                rowSpacing: 8

                Label { text: qsTr("远程单次请求数量") ; color: "#a1a1aa" }
                TextField {
                    id: pageSizeInput
                    objectName: "pageSizeInput"
                    Layout.fillWidth: true
                    text: panel.textValue("pageSize")
                    onTextEdited: panel.setValue("pageSize", text)
                }

                Label { text: qsTr("候选数量上限") ; color: "#a1a1aa" }
                TextField {
                    objectName: "candidateLimitInput"
                    Layout.fillWidth: true
                    text: panel.textValue("candidateLimit")
                    onTextEdited: panel.setValue("candidateLimit", text)
                }

                Label { text: qsTr("每分支页数上限") ; color: "#a1a1aa" }
                TextField {
                    objectName: "branchPageLimitInput"
                    Layout.fillWidth: true
                    text: panel.textValue("branchPageLimit")
                    onTextEdited: panel.setValue("branchPageLimit", text)
                }

                Label { text: qsTr("活动时间上限（秒）") ; color: "#a1a1aa" }
                TextField {
                    objectName: "activeTimeLimitInput"
                    Layout.fillWidth: true
                    text: panel.textValue("activeTimeLimitSeconds")
                    onTextEdited: panel.setValue("activeTimeLimitSeconds", text)
                }
            }

            Label {
                Layout.fillWidth: true
                visible: controller.hasDraftError && controller.errorField !== "expression"
                text: controller.inputErrorText
                color: "#fda4af"
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                Button {
                    objectName: "applyViewButton"
                    text: qsTr("应用筛选与排序")
                    enabled: controller.canApplyView
                    onClicked: controller.applyView()
                }
                Item { Layout.fillWidth: true }
                Label {
                    text: qsTr("时区：%1").arg(controller.timeZoneId)
                    color: "#71717a"
                    font.pixelSize: 11
                }
            }

            Item { Layout.preferredHeight: 8 }
        }
    }
}
