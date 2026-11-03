import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: root
    visible: true
    width: 1280
    height: 820
    minimumWidth: 820
    minimumHeight: 600
    title: qsTr("Danbooru Multi Search")
    color: "#15171c"
    property bool autocompleteReady: false

    function submitGallerySearch() {
        tagSuggestionController.closeSuggestions()
        searchController.search(
            tagsInput.text,
            ratingFilter.currentValue,
            pageSizeInput.value
        )
        tagsInput.forceActiveFocus()
    }

    function applySuggestion(index, searchWhenValid) {
        const replacement = tagSuggestionController.applySuggestion(
            tagsInput.text,
            tagsInput.cursorPosition,
            index
        )
        if (!replacement.changed)
            return false
        tagsInput.suppressAutocomplete = true
        tagsInput.text = replacement.text
        tagsInput.cursorPosition = replacement.cursor
        tagsInput.suppressAutocomplete = false
        tagsInput.forceActiveFocus()
        if (searchWhenValid && replacement.shouldSearch)
            submitGallerySearch()
        return true
    }

    function ratingColor(code) {
        if (code === "g") return "#3f8f5a"
        if (code === "s") return "#3976b8"
        if (code === "q") return "#bb7628"
        if (code === "e") return "#b64056"
        return "#626872"
    }

    FileDialog {
        id: tagCsvDialog
        objectName: "tagCsvDialog"
        title: qsTr("选择本地标签 CSV")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("CSV 文件 (*.csv)"), qsTr("所有文件 (*)")]
        onAccepted: tagSuggestionController.importCsv(selectedFile)
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
            const point = tagsInput.mapToItem(Overlay.overlay, 0, tagsInput.height + 2)
            x = point.x
            y = point.y
            width = tagsInput.width
        }

        onAboutToShow: {
            updatePosition()
            suggestionList.currentIndex = suggestionList.count > 0 ? 0 : -1
        }
        onClosed: {
            if (tagSuggestionController.isOpen)
                tagSuggestionController.closeSuggestions()
        }

        background: Rectangle {
            color: "#20242c"
            border.color: "#4b5362"
            radius: 6
        }

        contentItem: ListView {
            id: suggestionList
            objectName: "suggestionList"
            clip: true
            model: tagSuggestionController.suggestionsModel
            currentIndex: count > 0 ? 0 : -1
            implicitHeight: contentHeight

            onCountChanged: currentIndex = count > 0 ? 0 : -1

            highlight: Rectangle {
                color: "#344866"
                radius: 4
            }

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
                onClicked: root.applySuggestion(index, false)

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

                    Label {
                        text: suggestionRow.categoryText
                        color: "#93c5fd"
                    }

                    Label {
                        text: suggestionRow.postCountText
                        color: "#d4d4d8"
                    }
                }
            }
        }
    }

    Connections {
        target: tagSuggestionController

        function onOpenChanged() {
            if (tagSuggestionController.isOpen
                    && tagSuggestionController.suggestionsModel.count > 0) {
                suggestionPopup.updatePosition()
                suggestionPopup.open()
                tagsInput.forceActiveFocus()
            } else {
                suggestionPopup.close()
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 18
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Label {
                    text: qsTr("Danbooru Multi Search")
                    color: "#f4f4f5"
                    font.pixelSize: 25
                    font.bold: true
                }

                Label {
                    text: qsTr("M1 分页搜索结果画廊")
                    color: "#a1a1aa"
                    font.pixelSize: 14
                }
            }

            Label {
                text: qsTr("第 %1 页").arg(searchController.currentPage)
                color: "#d4d4d8"
                font.pixelSize: 17
                font.bold: true
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            TextField {
                id: tagsInput
                objectName: "tagsInput"
                Layout.fillWidth: true
                placeholderText: qsTr("输入一个或两个英文标签，例如 landscape 1girl")
                text: "landscape"
                selectByMouse: true
                property bool suppressAutocomplete: false

                function updateSuggestions() {
                    if (root.autocompleteReady && !suppressAutocomplete) {
                        tagSuggestionController.updateInput(text, cursorPosition)
                    }
                }

                onTextChanged: updateSuggestions()
                onCursorPositionChanged: updateSuggestions()
                Component.onCompleted: root.autocompleteReady = true

                Keys.onPressed: function(event) {
                    if (inputMethodComposing)
                        return
                    const popupHasItems = suggestionPopup.opened
                                          && suggestionList.count > 0
                    if (event.key === Qt.Key_Escape
                            && (suggestionPopup.opened
                                || tagSuggestionController.debouncePending
                                || tagSuggestionController.remoteBusy)) {
                        tagSuggestionController.closeSuggestions()
                        event.accepted = true
                    } else if (event.key === Qt.Key_Down && popupHasItems) {
                        suggestionList.currentIndex =
                            (suggestionList.currentIndex + 1) % suggestionList.count
                        event.accepted = true
                    } else if (event.key === Qt.Key_Up && popupHasItems) {
                        suggestionList.currentIndex =
                            (suggestionList.currentIndex - 1 + suggestionList.count)
                            % suggestionList.count
                        event.accepted = true
                    } else if ((event.key === Qt.Key_Return
                                || event.key === Qt.Key_Enter)) {
                        if (popupHasItems) {
                            root.applySuggestion(suggestionList.currentIndex, true)
                        } else {
                            root.submitGallerySearch()
                        }
                        event.accepted = true
                    }
                }
            }

            Button {
                id: importTagsButton
                objectName: "importTagsButton"
                text: tagSuggestionController.importBusy
                      ? qsTr("正在导入……")
                      : qsTr("导入标签库")
                enabled: !tagSuggestionController.importBusy
                onClicked: tagCsvDialog.open()
            }

            ComboBox {
                id: ratingFilter
                objectName: "ratingFilter"
                Layout.preferredWidth: 150
                textRole: "label"
                valueRole: "value"
                model: [
                    { "label": qsTr("全部分级"), "value": "all" },
                    { "label": "General", "value": "general" },
                    { "label": "Sensitive", "value": "sensitive" },
                    { "label": "Questionable", "value": "questionable" },
                    { "label": "Explicit", "value": "explicit" }
                ]
            }

            Label {
                text: qsTr("每页")
                color: "#d4d4d8"
            }

            SpinBox {
                id: pageSizeInput
                objectName: "pageSizeInput"
                from: 1
                to: 200
                value: 40
                editable: true
                Layout.preferredWidth: 96
            }

            Button {
                objectName: "searchButton"
                text: searchController.busy ? qsTr("重新搜索") : qsTr("搜索")
                onClicked: root.submitGallerySearch()
            }

            Button {
                objectName: "cancelButton"
                text: qsTr("取消")
                visible: searchController.busy
                onClicked: searchController.cancelCurrentRequest()
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            Label {
                objectName: "tagImportStatus"
                Layout.fillWidth: true
                text: tagSuggestionController.importStatusText
                color: tagSuggestionController.importBusy ? "#93c5fd" : "#a1a1aa"
                elide: Text.ElideRight
            }

            Label {
                objectName: "tagSuggestionNotice"
                visible: text.length > 0
                text: tagSuggestionController.noticeText
                color: "#fbbf24"
                elide: Text.ElideRight
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Label {
                Layout.fillWidth: true
                text: searchController.statusText
                color: searchController.state === "error" ? "#fda4af" : "#d4d4d8"
                wrapMode: Text.Wrap
            }

            Button {
                objectName: "retryButton"
                text: qsTr("重试失败请求")
                visible: searchController.canRetry
                onClicked: searchController.retry()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 380
            radius: 8
            color: "#0f1115"
            border.color: "#2d3139"
            clip: true

            GridView {
                id: galleryGrid
                objectName: "galleryGrid"
                anchors.fill: parent
                anchors.margins: 10
                clip: true
                model: searchController.postsModel
                cellWidth: Math.max(225, width / Math.max(1, Math.floor(width / 245)))
                cellHeight: 340
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}

                delegate: Rectangle {
                    id: card
                    objectName: "postCard_" + postId
                    required property int postId
                    required property string previewUrl
                    required property string scoreText
                    required property string favoriteText
                    required property string ratingCode
                    required property string ratingText
                    required property string dimensionsText
                    required property string fileExtension
                    required property string mediaLabel

                    width: GridView.view.cellWidth - 12
                    height: GridView.view.cellHeight - 12
                    radius: 7
                    color: "#191c23"
                    border.color: "#343944"

                    Image {
                        id: thumbnail
                        objectName: "thumbnail_" + card.postId
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 8
                        height: 220
                        source: card.previewUrl
                        fillMode: Image.PreserveAspectFit
                        asynchronous: true
                        cache: true

                        BusyIndicator {
                            anchors.centerIn: parent
                            running: thumbnail.status === Image.Loading
                            visible: running
                        }

                        Rectangle {
                            objectName: "thumbnailError_" + card.postId
                            anchors.fill: parent
                            color: "#242832"
                            visible: thumbnail.status === Image.Error

                            Label {
                                anchors.centerIn: parent
                                width: parent.width - 24
                                text: qsTr("此缩略图加载失败")
                                color: "#fda4af"
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.Wrap
                            }
                        }
                    }

                    Row {
                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.margins: 12
                        spacing: 5

                        Rectangle {
                            width: mediaText.implicitWidth + 12
                            height: 24
                            radius: 4
                            color: "#a23f52"
                            visible: card.mediaLabel.length > 0

                            Label {
                                id: mediaText
                                anchors.centerIn: parent
                                text: card.mediaLabel
                                color: "white"
                                font.bold: true
                            }
                        }

                    }

                    Rectangle {
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 12
                        width: ratingLabel.implicitWidth + 12
                        height: 24
                        radius: 4
                        color: root.ratingColor(card.ratingCode)

                        Label {
                            id: ratingLabel
                            anchors.centerIn: parent
                            text: card.ratingText
                            color: "white"
                            font.bold: true
                        }
                    }

                    ColumnLayout {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 10
                        spacing: 4

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("作品 #%1").arg(card.postId)
                            color: "#f4f4f5"
                            font.bold: true
                            elide: Text.ElideRight
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("得分 %1 · 收藏 %2").arg(card.scoreText).arg(card.favoriteText)
                            color: "#d4d4d8"
                            elide: Text.ElideRight
                        }

                        Label {
                            Layout.fillWidth: true
                            text: qsTr("%1 · %2").arg(card.fileExtension).arg(card.dimensionsText)
                            color: "#a1a1aa"
                            elide: Text.ElideRight
                        }
                    }
                }
            }

            Column {
                anchors.centerIn: parent
                spacing: 10
                visible: !searchController.busy
                         && searchController.postsModel.count === 0

                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: searchController.state === "empty"
                          ? qsTr("没有搜索结果")
                          : searchController.state === "cancelled"
                            ? qsTr("请求已取消")
                            : searchController.state === "error"
                              ? searchController.errorText
                              : qsTr("搜索结果将在这里显示")
                    color: searchController.state === "error" ? "#fda4af" : "#71717a"
                    width: Math.min(620, root.width - 100)
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
            }

            Rectangle {
                anchors.fill: parent
                color: "#990f1115"
                visible: searchController.busy

                Column {
                    anchors.centerIn: parent
                    spacing: 8

                    BusyIndicator {
                        anchors.horizontalCenter: parent.horizontalCenter
                        running: parent.parent.visible
                    }

                    Label {
                        text: qsTr("正在加载作品……")
                        color: "#f4f4f5"
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Button {
                objectName: "previousPageButton"
                text: qsTr("上一页")
                enabled: searchController.canPrevious
                onClicked: searchController.previousPage()
            }

            Label {
                Layout.fillWidth: true
                text: qsTr("第 %1 页 · %2 项作品")
                      .arg(searchController.currentPage)
                      .arg(searchController.postsModel.count)
                color: "#a1a1aa"
                horizontalAlignment: Text.AlignHCenter
            }

            Button {
                objectName: "refreshButton"
                text: qsTr("刷新")
                enabled: searchController.canRefresh
                onClicked: searchController.refresh()
            }

            Button {
                objectName: "nextPageButton"
                text: qsTr("下一页")
                enabled: searchController.canNext
                onClicked: searchController.nextPage()
            }
        }
    }
}
