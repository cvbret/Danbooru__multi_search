import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: panel
    property QtObject theme: AppTheme {}
    objectName: "queryEditorPanel"
    required property var controller
    required property var tagController
    property var selectedPath: null
    property string selectedKind: ""
    property var confirmedArtists: []
    property string suggestionTarget: ""
    property bool syncingText: false
    property bool suppressSuggestions: false
    property int observedEditorRevision: -1
    signal closeRequested()

    function parentPathForInsert() {
        if ((selectedKind === "and" || selectedKind === "or")
                && selectedPath !== null)
            return selectedPath
        return null
    }

    function syncAdvancedText() {
        if (syncingText)
            return
        syncingText = true
        advancedInput.text = controller.advancedText
        advancedInput.cursorPosition = advancedInput.text.length
        syncingText = false
    }

    function updateAdvancedSuggestions() {
        if (syncingText || suppressSuggestions || !advancedInput.activeFocus)
            return
        suggestionTarget = "advanced"
        tagController.updateQueryInput(
            advancedInput.text,
            advancedInput.cursorPosition
        )
    }

    function updateSimpleSuggestions(target, field) {
        if (suppressSuggestions || !field.activeFocus)
            return
        suggestionTarget = target
        tagController.updateInput(field.text, field.cursorPosition)
    }

    function selectedSuggestionIndex() {
        return suggestionList.currentIndex >= 0
               ? suggestionList.currentIndex : 0
    }

    function applySelectedSuggestion(requestedIndex, requestedName,
                                     requestedCategory) {
        const index = requestedIndex === undefined
                      ? selectedSuggestionIndex() : requestedIndex
        if (index < 0 || suggestionList.count === 0)
            return false
        const row = suggestionList.itemAtIndex(index)
        const suggestionName = requestedName !== undefined
                               ? requestedName
                               : row ? row.tagNameValue : ""
        const suggestionCategory = requestedCategory !== undefined
                                   ? requestedCategory
                                   : row ? row.categoryValue : -1
        if (suggestionTarget === "advanced") {
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
        const field = suggestionTarget === "artist" ? artistInput : visualTagInput
        const replacement = tagController.applySuggestion(
            field.text,
            field.cursorPosition,
            index
        )
        if (!replacement.changed)
            return false
        suppressSuggestions = true
        field.text = replacement.text.trim()
        field.cursorPosition = field.text.length
        if (suggestionTarget === "artist") {
            if (suggestionCategory === 1) {
                let exists = false
                const next = confirmedArtists.slice()
                for (let artistIndex = 0; artistIndex < next.length; ++artistIndex) {
                    if (next[artistIndex].name === suggestionName)
                        exists = true
                }
                if (!exists)
                    next.push({ "name": suggestionName, "category": 1 })
                confirmedArtists = next
                artistInput.text = ""
            } else {
                artistNotice.text = qsTr("画师快捷条件只接受分类已经确认为画师的标签。")
            }
        }
        suppressSuggestions = false
        field.forceActiveFocus()
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

    function addVisualTagFromInput() {
        const name = visualTagInput.text.trim()
        if (!name.length)
            return
        if (controller.addVisualTag(parentPathForInsert(), name))
            visualTagInput.clear()
    }

    function insertArtists() {
        if (!confirmedArtists.length)
            return
        if (controller.insertArtistGroup(
                parentPathForInsert(),
                confirmedArtists,
                artistMode.currentValue)) {
            confirmedArtists = []
            artistNotice.text = ""
        }
    }

    Component.onCompleted: {
        observedEditorRevision = controller.editorRevision
        syncAdvancedText()
    }
    Component.onDestruction: tagController.closeSuggestions()

    Connections {
        target: panel.controller
        function onDraftChanged() {
            if (panel.observedEditorRevision !== panel.controller.editorRevision) {
                panel.observedEditorRevision = panel.controller.editorRevision
                panel.selectedPath = null
                panel.selectedKind = ""
            }
            panel.syncAdvancedText()
        }
    }

    Connections {
        target: panel.tagController
        function onOpenChanged() {
            if (panel.tagController.isOpen
                    && panel.tagController.suggestionsModel.count > 0
                    && panel.suggestionTarget.length > 0) {
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

        function activeField() {
            if (panel.suggestionTarget === "artist")
                return artistInput
            if (panel.suggestionTarget === "visual")
                return visualTagInput
            return advancedInput
        }

        function updatePosition() {
            const field = activeField()
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
                required property int category
                readonly property string tagNameValue: tagName
                readonly property int categoryValue: category
                width: suggestionList.width
                height: 54
                highlighted: ListView.isCurrentItem
                hoverEnabled: true
                onHoveredChanged: {
                    if (hovered)
                        suggestionList.currentIndex = index
                }
                onClicked: panel.applySelectedSuggestion(
                    index,
                    suggestionRow.tagNameValue,
                    suggestionRow.categoryValue
                )

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
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: 16

            Label {
                Layout.fillWidth: true
                text: qsTr("输入英文标签；使用 AND、OR、NOT 组合条件。")
                color: theme.mutedText
                wrapMode: Text.Wrap
                font.pixelSize: 12
            }

            Label {
                text: qsTr("标签或查询表达式")
                color: theme.text
                font.bold: true
            }

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
                text: qsTr("放弃错误文本并恢复可视编辑")
                onClicked: controller.discardInvalidText()
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

            ActionButton {
                id: advancedToggle
                objectName: "advancedConditionsToggle"
                Layout.fillWidth: true
                checkable: true
                checked: false
                flat: true
                text: checked ? qsTr("收起可视条件与画师") : qsTr("展开可视条件与画师")
                onToggled: { if (!checked) tagController.closeSuggestions() }
            }
            ColumnLayout {
                Layout.fillWidth: true
                visible: advancedToggle.checked
                spacing: 16
                ActionButton {
                    objectName: "conditionalRankButton"
                    Layout.fillWidth: true
                    text: qsTr("在当前条件中查看热门")
                    enabled: controller.canExecuteConditionalRank
                    onClicked: {
                        tagController.closeSuggestions()
                        controller.executeRank(true)
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    color: theme.border
                }

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        Layout.fillWidth: true
                        text: qsTr("可视查询树")
                        color: theme.text
                        font.pixelSize: 16
                        font.bold: true
                    }
                    Label {
                        text: controller.visualEditingEnabled
                              ? qsTr("可编辑") : qsTr("已因文本错误锁定")
                        color: controller.visualEditingEnabled ? theme.success : theme.warning
                        font.pixelSize: 11
                    }
                }

                ListView {
                    id: queryTreeList
                    objectName: "queryTreeList"
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(250, Math.max(76, contentHeight))
                    clip: true
                    model: controller.queryTreeRows
                    spacing: 4

                    delegate: ItemDelegate {
                        required property int index
                        required property var modelData
                        readonly property var rowPath: modelData.path
                        readonly property string rowKind: modelData.kind
                        width: queryTreeList.width
                        height: 38
                        leftPadding: 10 + modelData.depth * 18
                        highlighted: JSON.stringify(panel.selectedPath)
                                     === JSON.stringify(rowPath)
                        onClicked: {
                            panel.selectedPath = rowPath
                            panel.selectedKind = rowKind
                        }
                        contentItem: RowLayout {
                            Label {
                                text: modelData.kind === "tag" ? "#" : "◆"
                                color: modelData.kind === "tag" ? theme.accentText : theme.purple
                            }
                            Label {
                                Layout.fillWidth: true
                                text: modelData.label
                                color: theme.text
                                elide: Text.ElideRight
                            }
                            Label {
                                visible: modelData.childCount > 0
                                text: modelData.childCount
                                color: theme.subtleText
                            }
                        }
                    }

                    Label {
                        anchors.centerIn: parent
                        visible: queryTreeList.count === 0
                        text: qsTr("查询树为空")
                        color: theme.subtleText
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    TextField {
                        id: visualTagInput
                        objectName: "visualTagInput"
                        Layout.fillWidth: true
                        enabled: controller.visualEditingEnabled
                        placeholderText: qsTr("输入普通标签")
                        onTextChanged: panel.updateSimpleSuggestions("visual", visualTagInput)
                        onCursorPositionChanged: panel.updateSimpleSuggestions("visual", visualTagInput)
                        Keys.onPressed: function(event) {
                            panel.handleSuggestionKeys(event, visualTagInput, false)
                            if (!event.accepted
                                    && (event.key === Qt.Key_Return
                                        || event.key === Qt.Key_Enter)
                                    && !visualTagInput.inputMethodComposing) {
                                panel.addVisualTagFromInput()
                                event.accepted = true
                            }
                        }
                    }
                    ActionButton {
                        objectName: "addVisualTagButton"
                        text: qsTr("添加")
                        enabled: controller.visualEditingEnabled
                                 && visualTagInput.text.trim().length > 0
                        onClicked: panel.addVisualTagFromInput()
                    }
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: panel.width >= 380 ? 3 : 2
                    columnSpacing: 6
                    rowSpacing: 6
                    ActionButton {
                        objectName: "moveNodeUpButton"
                        text: qsTr("上移")
                        enabled: controller.visualEditingEnabled && panel.selectedPath !== null
                        onClicked: controller.moveVisualNode(panel.selectedPath, -1)
                    }
                    ActionButton {
                        objectName: "moveNodeDownButton"
                        text: qsTr("下移")
                        enabled: controller.visualEditingEnabled && panel.selectedPath !== null
                        onClicked: controller.moveVisualNode(panel.selectedPath, 1)
                    }
                    ActionButton {
                        objectName: "removeNodeButton"
                        text: qsTr("删除")
                        enabled: controller.visualEditingEnabled && panel.selectedPath !== null
                        onClicked: controller.removeVisualNode(panel.selectedPath)
                    }
                    ActionButton {
                        objectName: "wrapNotButton"
                        text: qsTr("包裹 NOT")
                        enabled: controller.visualEditingEnabled && panel.selectedPath !== null
                        onClicked: controller.wrapVisualNot(panel.selectedPath)
                    }
                    ActionButton {
                        objectName: "unwrapNotButton"
                        text: qsTr("解除 NOT")
                        enabled: controller.visualEditingEnabled && panel.selectedKind === "not"
                        onClicked: controller.unwrapVisualNot(panel.selectedPath)
                    }
                    Item { Layout.fillWidth: true }
                }

                RowLayout {
                    Layout.fillWidth: true
                    ComboBox {
                        id: groupOperator
                        Layout.preferredWidth: 82
                        Layout.minimumWidth: 72
                        objectName: "groupOperatorInput"
                        textRole: "label"
                        valueRole: "value"
                        model: [
                            { "label": "AND", "value": "and" },
                            { "label": "OR", "value": "or" }
                        ]
                    }
                    TextField {
                        id: groupSiblingInput
                        objectName: "groupSiblingInput"
                        Layout.fillWidth: true
                        placeholderText: qsTr("与所选条件组合的标签")
                        enabled: controller.visualEditingEnabled
                    }
                    ActionButton {
                        objectName: "wrapGroupButton"
                        text: qsTr("建立组")
                        enabled: controller.visualEditingEnabled
                                 && panel.selectedPath !== null
                                 && groupSiblingInput.text.trim().length > 0
                        onClicked: {
                            if (controller.wrapVisualGroup(
                                    panel.selectedPath,
                                    groupOperator.currentValue,
                                    groupSiblingInput.text.trim(),
                                    true))
                                groupSiblingInput.clear()
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    color: theme.border
                }

                Label {
                    text: qsTr("多画师快捷条件")
                    color: theme.text
                    font.pixelSize: 16
                    font.bold: true
                }

                RowLayout {
                    Layout.fillWidth: true
                    TextField {
                        id: artistInput
                        objectName: "artistInput"
                        Layout.fillWidth: true
                        placeholderText: qsTr("从已确认的画师建议中选择")
                        enabled: controller.visualEditingEnabled
                        onTextChanged: panel.updateSimpleSuggestions("artist", artistInput)
                        onCursorPositionChanged: panel.updateSimpleSuggestions("artist", artistInput)
                        Keys.onPressed: function(event) {
                            panel.handleSuggestionKeys(event, artistInput, false)
                        }
                    }
                    ComboBox {
                        id: artistMode
                        Layout.preferredWidth: 120
                        Layout.minimumWidth: 110
                        objectName: "artistModeInput"
                        textRole: "label"
                        valueRole: "value"
                        model: [
                            { "label": qsTr("全部包含"), "value": "all" },
                            { "label": qsTr("任意包含"), "value": "any" },
                            { "label": qsTr("排除"), "value": "exclude" }
                        ]
                    }
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 6
                    Repeater {
                        model: panel.confirmedArtists
                        delegate: ActionButton {
                            required property var modelData
                            required property int index
                            text: modelData.name + "  ×"
                            onClicked: {
                                const next = panel.confirmedArtists.slice()
                                next.splice(index, 1)
                                panel.confirmedArtists = next
                            }
                        }
                    }
                }

                Label {
                    id: artistNotice
                    Layout.fillWidth: true
                    visible: text.length > 0
                    color: theme.warning
                    wrapMode: Text.Wrap
                }

                ActionButton {
                    objectName: "insertArtistGroupButton"
                    text: qsTr("插入画师条件")
                    enabled: controller.visualEditingEnabled
                             && panel.confirmedArtists.length > 0
                    onClicked: panel.insertArtists()
                }

                Label {
                    objectName: "tagSuggestionNotice"
                    Layout.fillWidth: true
                    visible: tagController.noticeText.length > 0
                    text: tagController.noticeText
                    color: theme.warning
                    wrapMode: Text.Wrap
                }

                Item { Layout.preferredHeight: 8 }
            }
        }
    }
}
