import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: panel
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
            color: "#20242c"
            border.color: "#4b5362"
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

            highlight: Rectangle { color: "#344866"; radius: 6 }

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
                    spacing: 10
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 1
                        Label {
                            Layout.fillWidth: true
                            text: suggestionRow.tagName
                            color: "#f4f4f5"
                            font.bold: true
                            elide: Text.ElideRight
                        }
                        Label {
                            Layout.fillWidth: true
                            text: suggestionRow.chineseNameText
                            color: "#a1a1aa"
                            elide: Text.ElideRight
                        }
                    }
                    Label { text: suggestionRow.categoryText; color: "#93c5fd" }
                    Label { text: suggestionRow.postCountText; color: "#d4d4d8" }
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
            spacing: 12

            Label {
                text: qsTr("开始查询")
                color: "#f4f4f5"
                font.pixelSize: 16
                font.bold: true
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 2
                rowSpacing: 8
                columnSpacing: 8

                Button {
                    id: standardSearchButton
                    objectName: "searchButton"
                    Layout.fillWidth: true
                    text: qsTr("普通查询")
                    enabled: controller.canExecuteStandard
                    onClicked: {
                        tagController.closeSuggestions()
                        controller.executeStandard()
                    }
                }
                Button {
                    objectName: "globalRankButton"
                    Layout.fillWidth: true
                    text: qsTr("近期热门")
                    enabled: controller.canExecuteGlobalRank
                    onClicked: {
                        tagController.closeSuggestions()
                        controller.executeRank(false)
                    }
                }
                Button {
                    objectName: "conditionalRankButton"
                    Layout.fillWidth: true
                    text: qsTr("在当前条件中查看热门")
                    enabled: controller.canExecuteConditionalRank
                    onClicked: {
                        tagController.closeSuggestions()
                        controller.executeRank(true)
                    }
                }
                Button {
                    objectName: "latestButton"
                    Layout.fillWidth: true
                    text: qsTr("最新作品")
                    enabled: controller.canExecuteLatest
                    onClicked: {
                        tagController.closeSuggestions()
                        controller.executeLatest()
                    }
                }
            }

            Label {
                text: qsTr("高级查询表达式")
                color: "#f4f4f5"
                font.bold: true
            }

            TextArea {
                id: advancedInput
                objectName: "tagsInput"
                Layout.fillWidth: true
                Layout.preferredHeight: 88
                placeholderText: qsTr("例如：(cat OR dog) AND NOT monochrome")
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
                Layout.fillWidth: true
                visible: controller.hasDraftError
                         && (controller.errorField === "expression"
                             || controller.errorStart >= 0)
                text: controller.inputErrorText
                      + (controller.errorStart >= 0
                         ? qsTr("  范围 [%1, %2)")
                           .arg(controller.errorStart)
                           .arg(controller.errorEnd)
                         : "")
                color: "#fda4af"
                wrapMode: Text.Wrap
            }

            Button {
                objectName: "discardInvalidTextButton"
                visible: !controller.visualEditingEnabled
                text: qsTr("放弃错误文本并恢复可视编辑")
                onClicked: controller.discardInvalidText()
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: "#343944"
            }

            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: qsTr("可视查询树")
                    color: "#f4f4f5"
                    font.pixelSize: 16
                    font.bold: true
                }
                Label {
                    text: controller.visualEditingEnabled
                          ? qsTr("可编辑") : qsTr("已因文本错误锁定")
                    color: controller.visualEditingEnabled ? "#86efac" : "#fbbf24"
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
                            color: modelData.kind === "tag" ? "#93c5fd" : "#c4b5fd"
                        }
                        Label {
                            Layout.fillWidth: true
                            text: modelData.label
                            color: "#e4e4e7"
                            elide: Text.ElideRight
                        }
                        Label {
                            visible: modelData.childCount > 0
                            text: modelData.childCount
                            color: "#71717a"
                        }
                    }
                }

                Label {
                    anchors.centerIn: parent
                    visible: queryTreeList.count === 0
                    text: qsTr("查询树为空")
                    color: "#71717a"
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
                Button {
                    objectName: "addVisualTagButton"
                    text: qsTr("添加")
                    enabled: controller.visualEditingEnabled
                             && visualTagInput.text.trim().length > 0
                    onClicked: panel.addVisualTagFromInput()
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 3
                columnSpacing: 6
                rowSpacing: 6
                Button {
                    objectName: "moveNodeUpButton"
                    text: qsTr("上移")
                    enabled: controller.visualEditingEnabled && panel.selectedPath !== null
                    onClicked: controller.moveVisualNode(panel.selectedPath, -1)
                }
                Button {
                    objectName: "moveNodeDownButton"
                    text: qsTr("下移")
                    enabled: controller.visualEditingEnabled && panel.selectedPath !== null
                    onClicked: controller.moveVisualNode(panel.selectedPath, 1)
                }
                Button {
                    objectName: "removeNodeButton"
                    text: qsTr("删除")
                    enabled: controller.visualEditingEnabled && panel.selectedPath !== null
                    onClicked: controller.removeVisualNode(panel.selectedPath)
                }
                Button {
                    objectName: "wrapNotButton"
                    text: qsTr("包裹 NOT")
                    enabled: controller.visualEditingEnabled && panel.selectedPath !== null
                    onClicked: controller.wrapVisualNot(panel.selectedPath)
                }
                Button {
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
                Button {
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
                color: "#343944"
            }

            Label {
                text: qsTr("多画师快捷条件")
                color: "#f4f4f5"
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
                    delegate: Button {
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
                color: "#fbbf24"
                wrapMode: Text.Wrap
            }

            Button {
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
                color: "#fbbf24"
                wrapMode: Text.Wrap
            }

            Item { Layout.preferredHeight: 8 }
        }
    }
}
