import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    objectName: "favoritesPage"
    signal transferRequested()
    signal artistCombinationsRequested()
    required property var controller
    required property var cacheController
    property QtObject theme: AppTheme {}
    readonly property var folderChoices: [{ "key": "", "name": qsTr("未分类") }].concat(controller.folders)
    readonly property var scopeChoices: [{ "key": "all", "name": qsTr("全部") }, { "key": "uncategorized", "name": qsTr("未分类") }].concat(controller.folders.map(function(f) { return { "key": "folder:" + f.key, "name": f.name } }))

    function applyControls() {
        const scope = folderFilter.currentValue || "all"
        controller.applyFilters({
            "scope": scope.indexOf("folder:") === 0 ? "folder" : scope,
            "folderKey": scope.indexOf("folder:") === 0 ? scope.slice(7) : "",
            "kind": kindFilter.currentValue,
            "rating": ratingFilter.currentValue,
            "sortField": sortField.currentValue,
            "sortDirection": sortDirection.currentValue,
            "exactTag": exactTag.text,
            "exactArtist": exactArtist.text
        })
    }

    function restorePosition() {
        grid.forceLayout()
        const plan = controller.takeRestorePlan(grid.width, grid.height, grid.originY, grid.contentHeight)
        if (!plan.available || !controller.restorePlanCurrent(plan.generation, plan.modelRevision))
            return
        grid.currentIndex = plan.index
        if (!controller.restorePlanCurrent(plan.generation, plan.modelRevision))
            return
        if (plan.exact || plan.index < 0) {
            grid.contentY = plan.contentY
            controller.finishRestorePlan(plan.generation)
            return
        }
        grid.positionViewAtIndex(plan.index, GridView.Beginning)
        grid.forceLayout()
        Qt.callLater(function() {
            if (!controller.restorePlanCurrent(plan.generation, plan.modelRevision))
                return
            const card = grid.itemAtIndex(plan.index)
            const maximum = Math.max(grid.originY, grid.originY + grid.contentHeight - grid.height)
            grid.contentY = Math.max(grid.originY, Math.min(card ? card.y - plan.anchorOffset : grid.originY, maximum))
            controller.finishRestorePlan(plan.generation)
        })
    }

    function scheduleRestorePosition() {
        restoreTimer.restart()
    }

    Timer {
        id: restoreTimer
        interval: 16
        onTriggered: {
            if (!page.visible)
                return
            // StackLayout lays out a newly visible page during the next polish.
            // Consume the one-shot plan only after its current geometry is ready.
            if (Math.abs(page.width - page.parent.width) > 0.01
                    || Math.abs(page.height - page.parent.height) > 0.01
                    || Math.abs(grid.width - page.width) > 0.01) {
                restart()
                return
            }
            page.restorePosition()
        }
    }

    FavoriteConfirmDialog { controller: page.controller; theme: page.theme }

    Dialog {
        id: folderDialog
        objectName: "favoriteFolderDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, parent ? parent.width - 32 : 440)
        modal: true
        property string frozenKey: ""
        property int frozenGeneration: -1
        title: frozenKey.length ? qsTr("修改文件夹名称") : qsTr("新建收藏文件夹")
        contentItem: ColumnLayout {
            spacing: 16
            TextField { id: folderName; objectName: "favoriteFolderNameInput"; Layout.fillWidth: true; placeholderText: qsTr("文件夹名称") }
            Label { Layout.fillWidth: true; text: page.controller.errorText; visible: text.length > 0; wrapMode: Text.Wrap; color: theme.error }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                ActionButton { text: qsTr("取消"); onClicked: folderDialog.close() }
                ActionButton {
                    objectName: "saveFavoriteFolderButton"
                    text: qsTr("保存")
                    enabled: folderName.text.trim().length > 0 && !favoriteController.busy
                    onClicked: {
                        if (page.controller.saveFolder(folderDialog.frozenKey, folderDialog.frozenGeneration, folderName.text))
                            folderDialog.close()
                    }
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Label { text: qsTr("本地收藏"); font.pixelSize: 25; font.bold: true; color: theme.text; Layout.fillWidth: true }
            ActionButton { objectName: "openFavoriteTransferButton"; text: qsTr("导入 / 导出"); onClicked: page.transferRequested() }
            ActionButton { objectName: "reloadFavoritesButton"; text: qsTr("刷新"); onClicked: page.controller.reload() }
            ActionButton {
                objectName: "createFavoriteFolderButton"
                text: qsTr("新建文件夹")
                onClicked: {
                    folderDialog.frozenKey = ""
                    folderDialog.frozenGeneration = page.controller.generation
                    folderName.text = ""
                    folderDialog.open()
                }
            }
        }

        ScrollView {
            objectName: "favoriteFilterScroll"
            Layout.fillWidth: true
            Layout.preferredHeight: page.height < 650 ? 170 : 216
            contentWidth: availableWidth
            clip: true
            Flow {
                width: parent.width
                spacing: 8
                ComboBox {
                    id: folderFilter
                    objectName: "favoriteFolderFilter"
                    width: Math.min(220, page.width - 24)
                    textRole: "name"; valueRole: "key"; model: page.scopeChoices
                    currentIndex: Math.max(0, indexOfValue(page.controller.formValues.scope === "folder" ? "folder:" + page.controller.formValues.folderKey : page.controller.formValues.scope))
                    onActivated: page.applyControls()
                }
                ComboBox {
                    id: kindFilter
                    objectName: "favoriteKindFilter"
                    width: 160
                    textRole: "name"; valueRole: "key"
                    model: [{"key":"all","name":qsTr("全部类型")},{"key":"post","name":qsTr("图片")},{"key":"artist","name":qsTr("画师")},{"key":"character","name":qsTr("角色")},{"key":"copyright","name":qsTr("作品系列")},{"key":"general","name":qsTr("普通标签")},{"key":"pool","name":qsTr("已保存作品池")}]
                    onActivated: page.applyControls()
                }
                ComboBox {
                    id: ratingFilter
                    objectName: "favoriteRatingFilter"
                    width: 160
                    textRole: "name"; valueRole: "key"
                    model: [{"key":"all","name":qsTr("全部分级")},{"key":"g","name":"General"},{"key":"s","name":"Sensitive"},{"key":"q","name":"Questionable"},{"key":"e","name":"Explicit"}]
                    onActivated: page.applyControls()
                }
                ComboBox {
                    id: sortField
                    objectName: "favoriteSortField"
                    width: 160
                    textRole: "name"; valueRole: "key"
                    model: [{"key":"favorited_at","name":qsTr("收藏时间")},{"key":"score","name":qsTr("作品得分")},{"key":"fav_count","name":qsTr("收藏数量")}]
                    onActivated: page.applyControls()
                }
                ComboBox {
                    id: sortDirection
                    objectName: "favoriteSortDirection"
                    width: 140
                    textRole: "name"; valueRole: "key"
                    model: [{"key":"descending","name":qsTr("降序")},{"key":"ascending","name":qsTr("升序")}]
                    onActivated: page.applyControls()
                }
                TextField { id: exactTag; objectName: "favoriteExactTagInput"; width: 200; placeholderText: qsTr("精确标签"); onAccepted: page.applyControls() }
                TextField { id: exactArtist; objectName: "favoriteExactArtistInput"; width: 200; placeholderText: qsTr("精确画师标签"); onAccepted: page.applyControls() }
                ActionButton { objectName: "applyFavoriteFiltersButton"; text: qsTr("应用筛选"); onClicked: page.applyControls() }
                ActionButton {
                    objectName: "renameFavoriteFolderButton"
                    text: qsTr("修改文件夹名称")
                    enabled: (folderFilter.currentValue || "").indexOf("folder:") === 0
                    property var pressedAction: null
                    onPressed: pressedAction = { "key": (folderFilter.currentValue || "").slice(7), "generation": page.controller.generation, "name": folderFilter.currentText }
                    onClicked: {
                        if (pressedAction) {
                            folderDialog.frozenKey = pressedAction.key
                            folderDialog.frozenGeneration = pressedAction.generation
                            folderName.text = pressedAction.name
                            folderDialog.open()
                        }
                        pressedAction = null
                    }
                }
                ActionButton {
                    objectName: "removeFavoriteFolderButton"
                    text: qsTr("删除文件夹，移入未分类")
                    enabled: (folderFilter.currentValue || "").indexOf("folder:") === 0
                    property var pressedAction: null
                    onPressed: pressedAction = { "key": (folderFilter.currentValue || "").slice(7), "generation": page.controller.generation }
                    onClicked: { if (pressedAction) page.controller.prepareDeleteFolder(pressedAction.key, false, pressedAction.generation); pressedAction = null }
                }
                ActionButton {
                    objectName: "removeFavoriteFolderContentsButton"
                    text: qsTr("删除文件夹及其中收藏")
                    enabled: (folderFilter.currentValue || "").indexOf("folder:") === 0
                    property var pressedAction: null
                    onPressed: pressedAction = { "key": (folderFilter.currentValue || "").slice(7), "generation": page.controller.generation }
                    onClicked: { if (pressedAction) page.controller.prepareDeleteFolder(pressedAction.key, true, pressedAction.generation); pressedAction = null }
                }
            }
        }

        Flow {
            Layout.fillWidth: true
            spacing: 8
            ActionButton { objectName: "openFavoriteArtistCombinationsButton"; text: qsTr("收藏画师 / 常用组合"); onClicked: page.artistCombinationsRequested() }
            Label { height: 40; verticalAlignment: Text.AlignVCenter; text: qsTr("已加载 %1 / %2 · 已选择 %3").arg(page.controller.loadedCount).arg(page.controller.totalCount).arg(page.controller.selectedCount); color: theme.secondaryText }
            ActionButton {
                objectName: "selectLoadedFavoritesButton"
                text: qsTr("全选已加载")
                enabled: page.controller.loadedCount > 0
                property int pressedGeneration: -1
                onPressed: pressedGeneration = page.controller.generation
                onClicked: page.controller.selectLoaded(pressedGeneration)
            }
            ActionButton { objectName: "clearCollectionSelectionButton"; text: qsTr("清除选择"); enabled: page.controller.selectedCount > 0; onClicked: page.controller.clearSelection() }
            ComboBox { id: moveTarget; objectName: "favoriteMoveTarget"; width: 180; textRole: "name"; valueRole: "key"; model: page.folderChoices }
            ActionButton {
                objectName: "moveSelectedFavoritesButton"
                text: qsTr("移动所选")
                enabled: page.controller.selectedCount > 0 && !favoriteController.busy
                property var pressedAction: null
                onPressed: pressedAction = { "target": moveTarget.currentValue || "", "selection": page.controller.selectionRevision, "generation": page.controller.generation }
                onClicked: { if (pressedAction) page.controller.prepareMove(pressedAction.target, pressedAction.selection, pressedAction.generation); pressedAction = null }
            }
            ActionButton {
                objectName: "deleteSelectedFavoritesButton"
                text: qsTr("取消所选本地收藏")
                enabled: page.controller.selectedCount > 0 && !favoriteController.busy
                property var pressedAction: null
                onPressed: pressedAction = { "selection": page.controller.selectionRevision, "generation": page.controller.generation }
                onClicked: { if (pressedAction) page.controller.prepareDelete(pressedAction.selection, pressedAction.generation); pressedAction = null }
            }
        }
        Label { objectName: "favoriteCollectionError"; Layout.fillWidth: true; visible: text.length > 0; text: page.controller.errorText; wrapMode: Text.Wrap; color: theme.error }

        GridView {
            id: grid
            objectName: "favoriteGrid"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 140
            model: page.controller.model
            clip: true
            cellWidth: Math.max(225, width / Math.max(1, Math.floor(width / 245)))
            cellHeight: 340
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}
            delegate: Loader {
                id: rowLoader
                required property int index
                readonly property var rowData: { const revision = page.controller.model.revision; return page.controller.rowAt(index) }
                readonly property bool selected: { const revision = page.controller.selectionRevision; return page.controller.isSelected(rowData.favoriteKey) }
                readonly property var favoriteState: { const generation = page.controller.generation; const busy = favoriteController.busy; return rowData.postKey ? page.controller.postState(rowData.postKey) : { "known": true, "saved": true } }
                width: grid.cellWidth - 12
                height: grid.cellHeight - 12
                sourceComponent: rowData.objectType === "post" ? postCard : objectCard
                Component {
                    id: postCard
                    PostCard {
                        width: rowLoader.width; height: rowLoader.height
                        index: rowLoader.index
                        postKey: rowLoader.rowData.postKey
                        titleText: rowLoader.rowData.displayName === rowLoader.rowData.postKey ? qsTr("作品 #%1").arg(rowLoader.rowData.postKey) : rowLoader.rowData.displayName
                        selectionKey: rowLoader.rowData.favoriteKey
                        actionRevision: page.controller.generation
                        selected: rowLoader.selected
                        favoriteKnown: rowLoader.favoriteState.known
                        favoriteSaved: rowLoader.favoriteState.saved
                        favoriteEnabled: rowLoader.favoriteState.known && !favoriteController.busy
                        previewUrl: rowLoader.rowData.previewUrl
                        scoreText: rowLoader.rowData.scoreText
                        favoriteText: rowLoader.rowData.favoriteText
                        ratingCode: rowLoader.rowData.ratingCode
                        ratingText: rowLoader.rowData.ratingText
                        dimensionsText: rowLoader.rowData.dimensionsText
                        fileExtension: rowLoader.rowData.fileExtension
                        mediaLabel: rowLoader.rowData.mediaLabel
                        cacheController: page.cacheController
                        theme: page.theme
                        onSelectionRequested: function(key, desired, revision) { page.controller.setSelected(key, desired, revision) }
                        onFavoriteRequested: function(key, desired, revision) { page.controller.setPostFavorite(key, desired, revision) }
                        onDetailRequested: function(key, revision, row) { page.controller.openItem(key, revision, grid.contentY, row, grid.width, grid.height, rowLoader.y - grid.contentY) }
                    }
                }
                Component {
                    id: objectCard
                    FavoriteObjectCard {
                        width: rowLoader.width; height: rowLoader.height
                        rowData: rowLoader.rowData
                        selected: rowLoader.selected
                        actionRevision: page.controller.generation
                        theme: page.theme
                        onSelectionRequested: function(key, desired, revision) { page.controller.setSelected(key, desired, revision) }
                        onOpenRequested: function(key, revision) {
                            page.controller.openItem(key, revision, grid.contentY, rowLoader.index,
                                grid.width, grid.height, rowLoader.y - grid.contentY)
                        }
                    }
                }
            }
            Label {
                anchors.centerIn: parent
                width: Math.min(500, parent.width - 32)
                text: page.controller.errorText.length ? qsTr("收藏读取失败，请刷新重试。") : qsTr("当前范围没有本地收藏。可以从浏览卡片或作品详情添加。")
                visible: page.controller.loadedCount === 0
                wrapMode: Text.Wrap
                horizontalAlignment: Text.AlignHCenter
                color: theme.mutedText
            }
        }
        ActionButton { objectName: "loadMoreFavoritesButton"; Layout.alignment: Qt.AlignHCenter; text: qsTr("加载更多"); visible: page.controller.canLoadMore; onClicked: page.controller.loadMore() }
    }
}
