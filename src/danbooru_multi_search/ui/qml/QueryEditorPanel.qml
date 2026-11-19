import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: panel
    property QtObject theme: AppTheme {}
    objectName: "queryEditorPanel"
    required property var controller
    required property var tagController
    required property var picker
    property bool syncingText: false
    property bool suppressSuggestions: false
    property bool externalSuggestionSuppressed: false
    signal closeRequested()

    function syncAdvancedText() {
        if (syncingText)
            return
        syncingText = true
        advancedInput.text = controller.advancedText
        advancedInput.cursorPosition = advancedInput.text.length
        syncingText = false
    }

    function updateAdvancedSuggestions() {
        if (syncingText || suppressSuggestions || externalSuggestionSuppressed || !advancedInput.activeFocus || !advancedInput.visible)
            return
        tagController.updateQueryInput(
            advancedInput.text,
            advancedInput.cursorPosition
        )
    }

    function selectedSuggestionIndex() {
        return suggestionList.currentIndex >= 0
               ? suggestionList.currentIndex : 0
    }

    function applySelectedSuggestion(requestedIndex) {
        const index = requestedIndex === undefined
                      ? selectedSuggestionIndex() : requestedIndex
        if (index < 0 || suggestionList.count === 0)
            return false
        const replacement = tagController.applyQuerySuggestion(
            advancedInput.text,
            advancedInput.cursorPosition,
            index
        )
        if (!replacement.changed)
            return false
        suppressSuggestions = true
        syncingText = true
        advancedInput.text = replacement.text
        advancedInput.cursorPosition = replacement.cursor
        controller.applyAdvancedText(replacement.text)
        advancedInput.cursorPosition = replacement.cursor
        syncingText = false
        suppressSuggestions = false
        advancedInput.forceActiveFocus()
        return true
    }

    function handleSuggestionKeys(event, field, executeWhenClosed) {
        const hasSuggestions = suggestionPopup.opened && suggestionList.count > 0
        if (event.key === Qt.Key_Escape) {
            if (suggestionPopup.opened
                    || tagController.isOpen
                    || tagController.debouncePending
                    || tagController.remoteBusy) {
                tagController.closeSuggestions()
                event.accepted = true
            } else {
                panel.closeRequested()
                event.accepted = true
            }
            return
        }
        if (event.key === Qt.Key_Down && hasSuggestions) {
            suggestionList.currentIndex =
                (suggestionList.currentIndex + 1) % suggestionList.count
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_Up && hasSuggestions) {
            suggestionList.currentIndex =
                (suggestionList.currentIndex - 1 + suggestionList.count)
                % suggestionList.count
            event.accepted = true
            return
        }
        if (event.key !== Qt.Key_Return && event.key !== Qt.Key_Enter)
            return
        if (field.inputMethodComposing)
            return
        if (hasSuggestions) {
            applySelectedSuggestion()
            event.accepted = true
        } else if (executeWhenClosed) {
            tagController.closeSuggestions()
            controller.executeStandard()
            event.accepted = true
        }
    }

    Component.onCompleted: syncAdvancedText()
    Component.onDestruction: tagController.closeSuggestions()
    Connections { target: panel.controller; function onDraftChanged() { panel.syncAdvancedText() } }
    Connections {
        target: panel.tagController
        function onOpenChanged() {
            if (panel.tagController.isOpen
                    && panel.tagController.suggestionsModel.count > 0
                    && advancedInput.activeFocus && advancedInput.visible) {
                suggestionPopup.updatePosition()
                suggestionPopup.open()
            } else {
                suggestionPopup.close()
            }
        }
    }

    Popup {
        id: suggestionPopup
        objectName: "suggestionPopup"
        parent: Overlay.overlay
        focus: false
        modal: false
        padding: 1
        closePolicy: Popup.CloseOnPressOutside
        height: Math.min(286, suggestionList.contentHeight + 2)


        function updatePosition() {
            const field = advancedInput
            if (!field)
                return
            const point = field.mapToItem(Overlay.overlay, 0, field.height + 2)
            x = point.x
            y = point.y
            width = Math.max(260, field.width)
        }

        onAboutToShow: {
            updatePosition()
            suggestionList.currentIndex = suggestionList.count > 0 ? 0 : -1
        }
        onClosed: {
            if (tagController.isOpen)
                tagController.closeSuggestions()
        }

        background: Rectangle {
            color: theme.surface
            border.color: theme.borderStrong
            radius: 8
        }

        contentItem: ListView {
            id: suggestionList
            objectName: "suggestionList"
            clip: true
            model: tagController.suggestionsModel
            currentIndex: count > 0 ? 0 : -1
            implicitHeight: contentHeight
            onCountChanged: currentIndex = count > 0 ? 0 : -1

            highlight: Rectangle { color: theme.selected; radius: theme.smallRadius }

            delegate: ItemDelegate {
                id: suggestionRow
                objectName: "suggestionRow_" + index
                required property int index
                required property string tagName
                required property string chineseNameText
                required property string categoryText
                required property string postCountText
                width: suggestionList.width
                height: 54
                highlighted: ListView.isCurrentItem
                hoverEnabled: true
                onHoveredChanged: {
                    if (hovered)
                        suggestionList.currentIndex = index
                }
                onClicked: panel.applySelectedSuggestion(index)

                contentItem: RowLayout {
                    spacing: theme.gap
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 1
                        Label {
                            Layout.fillWidth: true
                            text: suggestionRow.tagName
                            color: theme.text
                            font.bold: true
                            elide: Text.ElideRight
                        }
                        Label {
                            Layout.fillWidth: true
                            text: suggestionRow.chineseNameText
                            color: theme.mutedText
                            elide: Text.ElideRight
                        }
                    }
                    Label { text: suggestionRow.categoryText; color: theme.accentText }
                    Label { text: suggestionRow.postCountText; color: theme.secondaryText }
                }
            }
        }
    }

    ScrollView {
        id: form
        objectName: "queryEditorScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: form.availableWidth
            spacing: 8
            TagPickerPanel {
                id: tags
                Layout.fillWidth: true
                theme: panel.theme
                picker: panel.picker
                group: advancedToggle.checked ? groupMode.currentValue : "all"
            }
            Label {
                Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                visible: !picker.simpleEditable
                text: qsTr("当前手写查询：") + controller.advancedText; color: theme.accentText
            }
            ActionButton {
                objectName: "resetComplexTagDraft"
                visible: !picker.simpleEditable
                text: qsTr("清空表达式并重新选标签")
                property int pressedRevision: -1
                onPressed: pressedRevision = controller.editorRevision
                onClicked: if (pressedRevision === controller.editorRevision) picker.resetToPicker()
            }
            ActionButton {
                id: standardSearchButton
                objectName: "searchButton"
                Layout.fillWidth: true
                text: qsTr("搜索作品")
                primary: true
                enabled: controller.canExecuteStandard
                onClicked: {
                    tagController.closeSuggestions()
                    controller.executeStandard()
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                ActionButton {
                    objectName: "latestButton"
                    Layout.fillWidth: true
                    text: qsTr("最新作品")
                    enabled: controller.canExecuteLatest
                    onClicked: {
                        tagController.closeSuggestions()
                        controller.executeLatest()
                    }
                }
                ActionButton {
                    objectName: "globalRankButton"
                    Layout.fillWidth: true
                    text: qsTr("近期热门")
                    enabled: controller.canExecuteGlobalRank
                    onClicked: {
                        tagController.closeSuggestions()
                        controller.executeRank(false)
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Label { text: qsTr("本次最多查看"); color: theme.secondaryText }
                SpinBox {
                    objectName: "browseMaximum"; Layout.fillWidth: true
                    from: 1; to: 1000; editable: true
                    value: Number(controller.draftFormValues.candidateLimit || 1000)
                    onValueModified: {
                        const values = Object.assign({}, controller.draftFormValues)
                        values.candidateLimit = String(value)
                        controller.updateDraftForm(values)
                    }
                }
                Label { text: qsTr("件"); color: theme.secondaryText }
            }
            ActionButton {
                id: advancedToggle; objectName: "advancedConditionsToggle"; Layout.fillWidth: true
                checkable: true; flat: true; text: checked ? qsTr("收起高级条件") : qsTr("高级条件")
                onToggled: { if (!checked) tagController.closeSuggestions() }
            }
            ColumnLayout {
                Layout.fillWidth: true; spacing: 8; visible: advancedToggle.checked
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: qsTr("点击标签时"); color: theme.secondaryText }
                    ComboBox {
                        id: groupMode; objectName: "tagGroupMode"; Layout.fillWidth: true
                        textRole: "label"; valueRole: "key"
                        model: [{key:"all",label:qsTr("全部包含")},{key:"any",label:qsTr("包含任一")},{key:"exclude",label:qsTr("排除")}]
                    }
                }
                Label { text: qsTr("手写查询表达式"); color: theme.text; font.bold: true }
            TextArea {
                id: advancedInput
                objectName: "tagsInput"
                Layout.fillWidth: true
                Layout.preferredHeight: 104
                placeholderText: qsTr("例如：(cat OR dog) AND NOT monochrome")
                padding: 12
                color: theme.text
                placeholderTextColor: theme.subtleText
                background: Rectangle {
                    color: theme.input
                    radius: theme.smallRadius
                    border.width: advancedInput.activeFocus ? 2 : 1
                    border.color: advancedInput.activeFocus ? theme.accent : theme.borderStrong
                }
                wrapMode: TextEdit.Wrap
                selectByMouse: true
                onTextChanged: {
                    if (panel.syncingText)
                        return
                    panel.syncingText = true
                    controller.applyAdvancedText(text)
                    panel.syncingText = false
                    panel.updateAdvancedSuggestions()
                }
                onCursorPositionChanged: panel.updateAdvancedSuggestions()
                onActiveFocusChanged: {
                    if (activeFocus)
                        panel.updateAdvancedSuggestions()
                }
                Keys.onPressed: function(event) {
                    panel.handleSuggestionKeys(event, advancedInput, true)
                }
            }

            Label {
                objectName: "queryExpressionError"
                Layout.fillWidth: true
                visible: advancedInput.text.trim().length > 0
                         && controller.hasDraftError
                         && (controller.errorField === "expression"
                             || controller.errorStart >= 0)
                text: advancedInput.text.trim().length === 0 ? ""
                      : controller.inputErrorText
                        + (controller.errorStart >= 0
                           ? qsTr("（第 %1 个字符附近）").arg(controller.errorStart + 1)
                           : "")
                color: theme.error
                wrapMode: Text.Wrap
            }

            ActionButton {
                objectName: "discardInvalidTextButton"
                visible: advancedInput.text.trim().length > 0
                         && !controller.visualEditingEnabled
                text: qsTr("恢复上一次有效表达式")
                onClicked: controller.discardInvalidText()
            }

                ActionButton { text: qsTr("按表达式搜索"); objectName: "manualSearchButton"; enabled: controller.canExecuteStandard; onClicked: { tagController.closeSuggestions(); controller.executeStandard() } }
                ActionButton { objectName: "conditionalRankButton"; text: qsTr("在当前条件中查看热门"); enabled: controller.canExecuteConditionalRank; onClicked: controller.executeRank(true) }
                Label { Layout.fillWidth: true; visible: tagController.noticeText.length > 0; text: tagController.noticeText; wrapMode: Text.Wrap; color: theme.warning }
            }
        }
    }
}
