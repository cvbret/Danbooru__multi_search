import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: root
    visible: true
    x: sessionController.initialWindowX
    y: sessionController.initialWindowY
    width: sessionController.initialWindowWidth
    height: sessionController.initialWindowHeight
    minimumWidth: sessionController.minimumWindowWidth
    minimumHeight: sessionController.minimumWindowHeight
    title: qsTr("Danbooru Multi Search")
    color: "#15171c"
    property bool autocompleteReady: false
    property bool resetGalleryOnNextModel: false
    property bool controlUpdateInProgress: false
    property bool stateTrackingReady: false
    property bool windowStateReportPending: false

    function ratingIndex(value) {
        for (let index = 0; index < ratingFilter.count; ++index) {
            if (ratingFilter.model[index].value === value)
                return index
        }
        return 0
    }

    function applySearchControls(tags, rating, pageSize) {
        controlUpdateInProgress = true
        tagsInput.suppressAutocomplete = true
        tagSuggestionController.closeSuggestions()
        tagsInput.text = tags
        tagsInput.cursorPosition = tagsInput.text.length
        ratingFilter.currentIndex = ratingIndex(rating)
        pageSizeInput.value = pageSize
        tagsInput.suppressAutocomplete = false
        controlUpdateInProgress = false
    }

    function reportWindowState() {
        if (!stateTrackingReady
                || (root.visibility !== Window.Windowed
                    && root.visibility !== Window.Maximized))
            return
        sessionController.updateWindowState(
            root.x,
            root.y,
            root.width,
            root.height,
            root.visibility === Window.Maximized,
            root.visibility === Window.Minimized
        )
    }

    function scheduleWindowStateReport() {
        if (!stateTrackingReady || windowStateReportPending)
            return
        windowStateReportPending = true
        Qt.callLater(function() {
            windowStateReportPending = false
            reportWindowState()
        })
    }

    function reportGalleryPosition() {
        if (!stateTrackingReady
                || searchController.postsModel.count === 0)
            return
        const maximum = Math.max(
            galleryGrid.originY,
            galleryGrid.originY + galleryGrid.contentHeight - galleryGrid.height
        )
        const boundedContentY = Math.max(
            galleryGrid.originY,
            Math.min(galleryGrid.contentY, maximum)
        )
        let index = -1
        let firstY = Number.POSITIVE_INFINITY
        let firstX = Number.POSITIVE_INFINITY
        for (let candidate = 0;
             candidate < searchController.postsModel.count;
             ++candidate) {
            const candidateItem = galleryGrid.itemAtIndex(candidate)
            if (!candidateItem
                    || candidateItem.y + candidateItem.height <= boundedContentY
                    || candidateItem.y >= boundedContentY + galleryGrid.height)
                continue
            if (candidateItem.y < firstY
                    || (candidateItem.y === firstY && candidateItem.x < firstX)) {
                index = candidate
                firstY = candidateItem.y
                firstX = candidateItem.x
            }
        }
        if (index < 0)
            return
        const item = galleryGrid.itemAtIndex(index)
        const postId = searchController.postsModel.postIdAt(index)
        if (!item || postId < 1)
            return
        sessionController.updateGalleryPosition(
            postId,
            index,
            item.y - boundedContentY,
            boundedContentY,
            searchController.postsModel.revision
        )
    }

    function restoreHistoryPosition() {
        galleryGrid.forceLayout()
        const plan = sessionController.takeGalleryRestorePlan(
            galleryGrid.originY,
            galleryGrid.contentHeight,
            galleryGrid.height
        )
        if (!plan.available)
            return
        if (!sessionController.galleryRestorePlanCurrent(
                plan.token,
                plan.modelRevision)) {
            sessionController.finishGalleryRestorePlan(plan.token)
            return
        }
        if (plan.index < 0) {
            galleryGrid.currentIndex = -1
            galleryGrid.contentY = plan.absoluteContentY
            sessionController.finishGalleryRestorePlan(plan.token)
            return
        }
        if (!plan.hasAnchor) {
            galleryGrid.currentIndex = plan.index
            galleryGrid.contentY = plan.absoluteContentY
            sessionController.finishGalleryRestorePlan(plan.token)
            return
        }
        Qt.callLater(function() {
            if (!sessionController.galleryRestorePlanCurrent(
                    plan.token,
                    plan.modelRevision)) {
                sessionController.finishGalleryRestorePlan(plan.token)
                return
            }
            galleryGrid.currentIndex = plan.index
            galleryGrid.positionViewAtIndex(plan.index, GridView.Beginning)
            galleryGrid.forceLayout()
            const item = galleryGrid.itemAtIndex(plan.index)
            if (!item) {
                galleryGrid.contentY = plan.absoluteContentY
                sessionController.finishGalleryRestorePlan(plan.token)
                return
            }
            const maximum = Math.max(
                galleryGrid.originY,
                galleryGrid.originY + galleryGrid.contentHeight - galleryGrid.height
            )
            galleryGrid.contentY = Math.max(
                galleryGrid.originY,
                Math.min(item.y - plan.anchorOffset, maximum)
            )
            sessionController.finishGalleryRestorePlan(plan.token)
        })
    }

    function submitGallerySearch() {
        tagSuggestionController.closeSuggestions()
        sessionController.controlsEdited()
        resetGalleryOnNextModel = true
        searchController.search(
            tagsInput.text,
            ratingFilter.currentValue,
            pageSizeInput.value
        )
        tagsInput.forceActiveFocus()
    }

    Component.onCompleted: {
        applySearchControls(
            sessionController.initialTags,
            sessionController.initialRating,
            sessionController.initialPageSize
        )
        autocompleteReady = true
        if (sessionController.startMaximized) {
            showMaximized()
            Qt.callLater(function() {
                stateTrackingReady = true
                scheduleWindowStateReport()
            })
        } else {
            stateTrackingReady = true
            scheduleWindowStateReport()
        }
    }

    onXChanged: scheduleWindowStateReport()
    onYChanged: scheduleWindowStateReport()
    onWidthChanged: scheduleWindowStateReport()
    onHeightChanged: scheduleWindowStateReport()
    onVisibilityChanged: scheduleWindowStateReport()
    onClosing: {
        windowStateReportPending = false
        reportWindowState()
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

    function openPostDetails(row) {
        galleryGrid.currentIndex = row
        tagSuggestionController.closeSuggestions()
        detailController.openPost(
            row,
            galleryGrid.contentY,
            galleryGrid.currentIndex,
            galleryGrid.width,
            galleryGrid.height,
            root.width,
            root.height
        )
    }

    function returnToGallery() {
        if (!detailController.closeDetail())
            return
        Qt.callLater(root.restoreGallerySnapshot)
    }

    function restoreGallerySnapshot() {
        galleryGrid.forceLayout()
        const plan = detailController.takeGalleryRestorePlan(
            searchController.postsModel,
            galleryGrid.width,
            galleryGrid.height,
            root.width,
            root.height,
            galleryGrid.originY,
            galleryGrid.contentHeight
        )
        if (!plan.available)
            return
        galleryGrid.currentIndex = plan.index
        galleryGrid.contentY = plan.contentY
    }

    function searchFromDetailTag(tagName) {
        tagsInput.suppressAutocomplete = true
        tagsInput.text = tagName
        tagsInput.cursorPosition = tagsInput.text.length
        tagsInput.suppressAutocomplete = false
        sessionController.controlsEdited()
        galleryGrid.currentIndex = -1
        galleryGrid.positionViewAtBeginning()
        resetGalleryOnNextModel = true
        searchController.search(
            tagName,
            ratingFilter.currentValue,
            pageSizeInput.value
        )
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
        id: historyPopup
        objectName: "historyPopup"
        parent: Overlay.overlay
        width: Math.min(620, root.width - 48)
        height: Math.min(420, root.height - 80)
        anchors.centerIn: parent
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        background: Rectangle {
            color: "#20242c"
            border.color: "#4b5362"
            radius: 8
        }

        contentItem: ColumnLayout {
            spacing: 8

            Label {
                text: qsTr("最近查询")
                color: "#f4f4f5"
                font.pixelSize: 20
                font.bold: true
            }

            ListView {
                id: historyList
                objectName: "historyList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: sessionController.historyModel

                delegate: ItemDelegate {
                    required property int index
                    required property string summaryText
                    width: historyList.width
                    text: summaryText
                    onClicked: {
                        sessionController.selectHistory(index)
                        historyPopup.close()
                        tagsInput.forceActiveFocus()
                    }
                }

                Label {
                    anchors.centerIn: parent
                    visible: historyList.count === 0
                    text: qsTr("暂无查询历史")
                    color: "#71717a"
                }
            }

            RowLayout {
                Layout.fillWidth: true

                Button {
                    objectName: "clearHistoryButton"
                    text: qsTr("清理历史")
                    enabled: historyList.count > 0
                    onClicked: {
                        sessionController.clearHistory()
                        historyPopup.close()
                    }
                }

                Item { Layout.fillWidth: true }

                Button {
                    text: qsTr("关闭")
                    onClicked: historyPopup.close()
                }
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

    Popup {
        id: imageCachePopup
        objectName: "imageCachePopup"
        parent: Overlay.overlay
        width: Math.min(460, root.width - 48)
        modal: true
        focus: true
        anchors.centerIn: parent
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        onAboutToShow: {
            cacheLimitInput.value = Math.round(
                imageCacheController.maximumBytes / 1048576
            )
            imageCacheController.refreshUsage()
        }

        background: Rectangle {
            color: "#20242c"
            border.color: "#4b5362"
            radius: 8
        }

        contentItem: ColumnLayout {
            spacing: 10

            Label {
                text: qsTr("图片缓存")
                color: "#f4f4f5"
                font.pixelSize: 20
                font.bold: true
            }

            Label {
                objectName: "imageCacheUsageLabel"
                text: qsTr("当前占用：%1 MiB")
                      .arg((imageCacheController.currentBytes / 1048576).toFixed(2))
                color: "#d4d4d8"
            }

            RowLayout {
                Layout.fillWidth: true

                Label {
                    text: qsTr("上限（MiB）")
                    color: "#d4d4d8"
                }

                SpinBox {
                    id: cacheLimitInput
                    objectName: "imageCacheLimitInput"
                    from: 64
                    to: 16384
                    value: 512
                    editable: true
                    Layout.fillWidth: true
                }

                Button {
                    objectName: "applyImageCacheLimitButton"
                    text: qsTr("保存上限")
                    onClicked: imageCacheController.setMaximumBytes(
                        cacheLimitInput.value * 1048576
                    )
                }
            }

            Label {
                Layout.fillWidth: true
                visible: text.length > 0
                text: imageCacheController.statusText
                color: "#fbbf24"
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true

                Button {
                    objectName: "clearImageCacheButton"
                    text: qsTr("清理图片缓存")
                    onClicked: imageCacheController.clearCache()
                }

                Item { Layout.fillWidth: true }

                Button {
                    text: qsTr("关闭")
                    onClicked: imageCachePopup.close()
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

    Connections {
        target: detailController

        function onTagSearchRequested(tagName) {
            root.searchFromDetailTag(tagName)
        }
    }

    Connections {
        target: sessionController

        function onControlsRequested(tags, rating, pageSize) {
            root.applySearchControls(tags, rating, pageSize)
        }

        function onGalleryRestoreReady() {
            Qt.callLater(root.restoreHistoryPosition)
        }
    }

    Connections {
        target: searchController

        function onPageRequestCompleted() {
            Qt.callLater(root.reportGalleryPosition)
        }
    }

    Connections {
        target: searchController.postsModel

        function onModelReset() {
            if (root.resetGalleryOnNextModel) {
                galleryGrid.currentIndex = -1
                galleryGrid.positionViewAtBeginning()
                root.resetGalleryOnNextModel = false
            }
            Qt.callLater(root.reportGalleryPosition)
        }
    }

    StackLayout {
        id: pageStack
        anchors.fill: parent
        currentIndex: detailController.isOpen ? 1 : 0

        Item {
            id: galleryPage

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
                text: sessionController.initialTags
                selectByMouse: true
                property bool suppressAutocomplete: false

                function updateSuggestions() {
                    if (root.autocompleteReady && !suppressAutocomplete) {
                        tagSuggestionController.updateInput(text, cursorPosition)
                    }
                }

                onTextChanged: updateSuggestions()
                onCursorPositionChanged: updateSuggestions()
                onTextEdited: {
                    if (!root.controlUpdateInProgress)
                        sessionController.controlsEdited()
                }

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

            Button {
                objectName: "historyButton"
                text: qsTr("最近查询")
                onClicked: historyPopup.open()
            }

            Button {
                objectName: "imageCacheButton"
                text: qsTr("图片缓存")
                onClicked: imageCachePopup.open()
            }

            Button {
                objectName: "restoreQueryButton"
                text: qsTr("恢复查询")
                enabled: sessionController.canRestoreSelected
                         && !searchController.busy
                onClicked: {
                    tagSuggestionController.closeSuggestions()
                    root.resetGalleryOnNextModel = false
                    sessionController.restoreSelected()
                }
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
                onActivated: {
                    if (!root.controlUpdateInProgress)
                        sessionController.controlsEdited()
                }
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
                onValueModified: {
                    if (!root.controlUpdateInProgress)
                        sessionController.controlsEdited()
                }
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
                onClicked: {
                    sessionController.cancelRestore()
                    searchController.cancelCurrentRequest()
                }
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

            Label {
                objectName: "sessionNotice"
                visible: text.length > 0
                text: sessionController.noticeText
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
                onContentYChanged: root.reportGalleryPosition()
                onCurrentIndexChanged: root.reportGalleryPosition()
                onWidthChanged: Qt.callLater(root.reportGalleryPosition)
                onHeightChanged: Qt.callLater(root.reportGalleryPosition)
                onContentHeightChanged: Qt.callLater(root.reportGalleryPosition)

                delegate: Rectangle {
                    id: card
                    objectName: "postCard_" + postId
                    required property int index
                    required property int postId
                    required property string previewUrl
                    required property string scoreText
                    required property string favoriteText
                    required property string ratingCode
                    required property string ratingText
                    required property string dimensionsText
                    required property string fileExtension
                    required property string mediaLabel
                    property int thumbnailRebuildToken: 0
                    property bool thumbnailFailed: false
                    property bool thumbnailLoading: false

                    width: GridView.view.cellWidth - 12
                    height: GridView.view.cellHeight - 12
                    radius: 7
                    color: "#191c23"
                    border.color: "#343944"

                    function rebuildThumbnail() {
                        const token = ++thumbnailRebuildToken
                        thumbnailFailed = false
                        thumbnailLoading = card.previewUrl.length > 0
                        thumbnailLoader.active = false
                        Qt.callLater(function() {
                            if (token !== thumbnailRebuildToken)
                                return
                            thumbnailLoader.active = card.previewUrl.length > 0
                        })
                    }

                    function handleThumbnailStatus(token, url, cacheGeneration, status) {
                        if (token !== thumbnailRebuildToken || url !== card.previewUrl)
                            return
                        if (status === Image.Ready) {
                            thumbnailLoading = false
                            const action = imageCacheController.decodeSuccessActionForGeneration(
                                url,
                                cacheGeneration
                            )
                            if (action === "accepted") {
                                thumbnailFailed = false
                            } else if (action === "stopped") {
                                thumbnailFailed = true
                            }
                        } else if (status === Image.Error) {
                            thumbnailLoading = false
                            const finalUrl = imageCacheController.finalUrlFor(url)
                            const action = imageCacheController.decodeFailureActionForGeneration(
                                url,
                                finalUrl,
                                cacheGeneration
                            )
                            if (action === "retry") {
                                rebuildThumbnail()
                            } else if (action === "stopped"
                                       || action === "failed") {
                                thumbnailFailed = true
                            }
                        }
                    }

                    function retryThumbnail() {
                        imageCacheController.prepareManualRetry(
                            card.previewUrl,
                            imageCacheController.finalUrlFor(card.previewUrl)
                        )
                        rebuildThumbnail()
                    }

                    Component.onCompleted: rebuildThumbnail()
                    onPreviewUrlChanged: rebuildThumbnail()
                    Component.onDestruction: {
                        ++thumbnailRebuildToken
                        thumbnailLoader.active = false
                    }

                    TapHandler {
                        onTapped: root.openPostDetails(card.index)
                    }

                    Loader {
                        id: thumbnailLoader
                        objectName: "thumbnailLoader_" + card.postId
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 8
                        height: 220
                        active: false
                        asynchronous: false
                        sourceComponent: thumbnailComponent
                    }

                    Component {
                        id: thumbnailComponent

                        Image {
                            objectName: "thumbnail_" + card.postId
                            source: ""
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            cache: false
                            property int frozenToken: 0
                            property int frozenCacheGeneration: 0
                            property string frozenUrl: ""

                            Component.onCompleted: {
                                frozenToken = card.thumbnailRebuildToken
                                frozenCacheGeneration = imageCacheController.cacheGeneration
                                frozenUrl = card.previewUrl
                                source = frozenUrl
                            }

                            onStatusChanged: card.handleThumbnailStatus(
                                frozenToken,
                                frozenUrl,
                                frozenCacheGeneration,
                                status
                            )
                        }
                    }

                    BusyIndicator {
                        anchors.centerIn: thumbnailLoader
                        running: card.thumbnailLoading
                        visible: running
                    }

                    Rectangle {
                        objectName: "thumbnailError_" + card.postId
                        anchors.fill: thumbnailLoader
                        color: "#242832"
                        visible: card.thumbnailFailed

                        Column {
                            anchors.centerIn: parent
                            width: parent.width - 24
                            spacing: 8
                            Label {
                                width: parent.width
                                text: qsTr("此缩略图加载失败")
                                color: "#fda4af"
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.Wrap
                            }

                            Button {
                                objectName: "thumbnailRetry_" + card.postId
                                anchors.horizontalCenter: parent.horizontalCenter
                                text: qsTr("重试")
                                onClicked: card.retryThumbnail()
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

        DetailPage {
            controller: detailController
            onBackRequested: root.returnToGallery()
            onImportTagsRequested: tagCsvDialog.open()
        }
    }

    Shortcut {
        objectName: "detailEscapeShortcut"
        enabled: detailController.isOpen
        sequence: StandardKey.Cancel
        onActivated: root.returnToGallery()
    }
}
