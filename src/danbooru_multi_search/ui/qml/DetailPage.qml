import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    property QtObject theme: AppTheme {}
    objectName: "detailPage"

    required property var favorites
    required property var controller
    required property var downloads
    property bool complexQueryActionsEnabled: false
    property string backLabel: qsTr("返回画廊")
    readonly property bool wideLayout: width >= 980
    onWideLayoutChanged: { if (wideLayout) informationDrawer.close() }
    signal backRequested()
    signal importTagsRequested()
    signal addTagToQueryRequested(string tagName)
    signal downloadPrepared(string token)
    signal poolsRequested(string key, int revision)

    function syncMedia() {
        if (controller.mediaUrl.length === 0) {
            imageViewer.clearMedia()
            return
        }
        imageViewer.loadCandidate(
            controller.mediaUrl,
            controller.mediaComponent,
            controller.mediaGeneration,
            controller.candidateAttempt
        )
    }

    function retryCurrentMedia() {
        const url = controller.mediaUrl
        if (url.length > 0) {
            imageCacheController.prepareManualRetry(
                url,
                imageCacheController.finalUrlFor(url)
            )
        }
        controller.retryMedia()
    }

    Connections {
        target: controller

        function onOpenChanged() {
            if (!controller.isOpen)
                informationDrawer.close()
        }

        function onMediaChanged() {
            page.syncMedia()
        }
    }

    Component.onCompleted: syncMedia()

    Drawer {
        id: informationDrawer
        objectName: "detailInformationDrawer"
        parent: Overlay.overlay
        width: Math.min(400, page.width * 0.9)
        height: parent ? parent.height : page.height
        edge: Qt.RightEdge
        modal: true
        padding: 16
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 16

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            ActionButton {
                objectName: "detailBackButton"
                text: page.backLabel
                onClicked: page.backRequested()
            }
            Label {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: qsTr("作品 #%1").arg(controller.postIdText)
                color: theme.text
                font.bold: true
                font.pixelSize: 23
                elide: Text.ElideRight
            }
            ActionButton {
                objectName: "detailInformationButton"
                text: qsTr("作品信息")
                visible: !page.wideLayout
                onClicked: informationDrawer.open()
            }
            ActionButton {
                objectName: "detailImportTagsButton"
                text: qsTr("标签库")
                flat: true
                onClicked: page.importTagsRequested()
            }
        }

        Label {
            Layout.fillWidth: true
            visible: favoriteController.noticeText.length > 0
            text: favoriteController.noticeText
            color: theme.error
            wrapMode: Text.Wrap
        }

        Label {
            objectName: "detailDownloadNotice"
            Layout.fillWidth: true
            visible: text.length > 0
            text: page.downloads.errorText || page.downloads.noticeText
            color: page.downloads.errorText.length ? theme.error : theme.secondaryText
            wrapMode: Text.Wrap
        }

        Flow {
            objectName: "detailActions"
            Layout.fillWidth: true
            spacing: 8
            ActionButton {
                objectName: "detailPoolsButton"
                text: qsTr("所属作品池")
                property var pressedAction: null
                onPressed: pressedAction = {"key": poolNavigation.detailPostKey, "revision": poolNavigation.detailRevision}
                onClicked: { if (pressedAction) page.poolsRequested(pressedAction.key, pressedAction.revision); pressedAction = null }
            }
            ActionButton {
                objectName: "detailDownloadButton"
                text: qsTr("下载原文件")
                enabled: page.downloads.detailDownloadAvailable
                property var pressedAction: null
                onPressed: pressedAction = {"key": page.downloads.detailPostKey, "revision": page.downloads.detailRevision}
                onClicked: {
                    if (pressedAction) {
                        const token = page.downloads.captureDetailPost(pressedAction.key, pressedAction.revision)
                        if (token.length) page.downloadPrepared(token)
                    }
                    pressedAction = null
                }
            }
            Label {
                width: Math.min(360, page.width - 24)
                visible: !page.downloads.detailDownloadAvailable
                text: qsTr("没有原文件地址，不能用预览图代替下载。")
                color: theme.secondaryText
                wrapMode: Text.Wrap
            }
            ActionButton {
                text: qsTr("重试收藏状态")
                visible: favoriteController.noticeText.length > 0
                enabled: !favoriteController.busy
                onClicked: favoriteController.retryStates()
            }
            ActionButton {
                objectName: "detailFavoriteButton"
                text: !favorites.postState.known ? qsTr("本地收藏状态未知")
                      : favorites.postState.saved ? qsTr("取消本地收藏") : qsTr("本地收藏")
                enabled: favorites.postState.enabled
                property var pressedAction: null
                onPressed: pressedAction = {
                    "key": favorites.postKey,
                    "revision": favorites.revision,
                    "desired": !favorites.postState.saved
                }
                onClicked: {
                    if (pressedAction)
                        favorites.setPostFavorite(pressedAction.key,
                            pressedAction.revision, pressedAction.desired)
                    pressedAction = null
                }
            }
            ActionButton {
                objectName: "detailPreviewButton"
                text: qsTr("预览图")
                checkable: true
                checked: controller.requestedQuality === "preview"
                onClicked: controller.requestQuality("preview")
            }
            ActionButton {
                objectName: "detailLargeButton"
                text: qsTr("较大图片")
                checkable: true
                checked: controller.requestedQuality === "large"
                onClicked: controller.requestQuality("large")
            }
            ActionButton {
                objectName: "detailOriginalButton"
                text: qsTr("原图")
                checkable: true
                checked: controller.requestedQuality === "original"
                onClicked: controller.requestQuality("original")
            }
            ActionButton {
                objectName: "detailFitWindowButton"
                text: qsTr("适应窗口")
                onClicked: imageViewer.fitWindow()
            }
            ActionButton {
                objectName: "detailFitWidthButton"
                text: qsTr("适应宽度")
                onClicked: imageViewer.fitWidth()
            }
            ActionButton {
                objectName: "detailActualSizeButton"
                text: "100%"
                onClicked: imageViewer.actualSize()
            }
            ActionButton {
                objectName: "detailZoomOutButton"
                width: 40
                leftPadding: 4
                rightPadding: 4
                text: "−"
                onClicked: imageViewer.zoomAt(
                    1 / 1.15,
                    imageViewer.width / 2,
                    imageViewer.height / 2
                )
            }
            ActionButton {
                objectName: "detailZoomInButton"
                width: 40
                leftPadding: 4
                rightPadding: 4
                text: "+"
                onClicked: imageViewer.zoomAt(
                    1.15,
                    imageViewer.width / 2,
                    imageViewer.height / 2
                )
            }
        }

        RowLayout {
            id: detailBody
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 20

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 6

                ImageViewer {
                    theme: page.theme
                    id: imageViewer
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    onCandidateSucceeded: function(generation, attempt, url,
                                                    pixelWidth, pixelHeight) {
                        if (controller.reportMediaSuccess(generation, attempt, url)) {
                            acceptDecodedDimensions(
                                generation,
                                attempt,
                                url,
                                pixelWidth,
                                pixelHeight
                            )
                        }
                    }

                    onCandidateFailed: function(generation, attempt, url) {
                        controller.reportMediaFailure(generation, attempt, url)
                    }

                    onCandidateStopped: function(generation, attempt, url) {
                        controller.reportMediaStopped(generation, attempt, url)
                    }
                }

                RowLayout {
                    Layout.fillWidth: true

                    BusyIndicator {
                        running: controller.mediaState === "loading"
                                 || controller.mediaState === "fallback"
                        visible: running
                    }

                    Label {
                        Layout.fillWidth: true
                        text: controller.mediaMessage
                        color: controller.mediaState === "error" ? theme.error : theme.secondaryText
                        wrapMode: Text.Wrap
                    }

                    Label {
                        Layout.maximumWidth: Math.max(140, imageViewer.width * 0.48)
                        wrapMode: Text.Wrap
                        font.pixelSize: 12
                        text: qsTr("请求：%1 · 已加载：%2 · %3%")
                              .arg(controller.requestedQualityText)
                              .arg(controller.actualQualityText)
                              .arg((imageViewer.zoomScale * 100).toFixed(1))
                        color: theme.mutedText
                    }

                    ActionButton {
                        objectName: "detailRetryMediaButton"
                        visible: controller.canRetryMedia
                        text: qsTr("重试图片")
                        onClicked: page.retryCurrentMedia()
                    }
                }
            }

            Rectangle {
                id: informationPanel
                objectName: "detailInformationPanel"
                parent: page.wideLayout ? detailBody : informationDrawer.contentItem
                anchors.fill: page.wideLayout ? null : parent
                Layout.preferredWidth: 340
                Layout.minimumWidth: 300
                Layout.fillHeight: true
                color: theme.card
                border.color: theme.border
                radius: 7

                ScrollView {
                    objectName: "detailInformationScroll"
                    anchors.fill: parent
                    anchors.margins: 20
                    clip: true
                    contentWidth: availableWidth

                    ColumnLayout {
                        width: Math.max(260, parent.width - 8)
                        spacing: 16

                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                Layout.fillWidth: true
                                text: qsTr("作品信息")
                                color: theme.text
                                font.bold: true
                                font.pixelSize: 18
                            }
                            ActionButton {
                                visible: !page.wideLayout
                                text: qsTr("关闭")
                                flat: true
                                onClicked: informationDrawer.close()
                            }
                        }

                        Label {
                            text: qsTr("Danbooru 收录时间")
                            color: theme.mutedText
                        }
                        Label {
                            Layout.fillWidth: true
                            text: controller.createdAtText
                            color: theme.text
                            wrapMode: Text.Wrap
                        }

                        Label {
                            text: qsTr("尺寸与文件")
                            color: theme.mutedText
                        }
                        Label {
                            Layout.fillWidth: true
                            text: qsTr("%1 · %2 · %3（%4）")
                                  .arg(controller.dimensionsText)
                                  .arg(controller.fileExtensionText)
                                  .arg(controller.fileSizeBytesText)
                                  .arg(controller.fileSizeReadableText)
                            color: theme.text
                            wrapMode: Text.Wrap
                        }

                        Label {
                            objectName: "detailFormatStatus"
                            Layout.fillWidth: true
                            visible: controller.formatStatusText.length > 0
                            text: controller.formatStatusText
                            color: controller.formatState === "unknown"
                                   ? theme.warning
                                   : theme.error
                            wrapMode: Text.Wrap
                        }

                        Label {
                            text: qsTr("内容与人气")
                            color: theme.mutedText
                        }
                        Label {
                            Layout.fillWidth: true
                            text: qsTr("%1 · 得分 %2 · 收藏 %3")
                                  .arg(controller.ratingText)
                                  .arg(controller.scoreText)
                                  .arg(controller.favoriteText)
                            color: theme.text
                            wrapMode: Text.Wrap
                        }

                        Label {
                            text: qsTr("Danbooru 地址")
                            color: theme.mutedText
                        }
                        TextArea {
                            objectName: "detailPostUrlText"
                            Layout.fillWidth: true
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.WrapAnywhere
                            textFormat: Text.PlainText
                            text: controller.postUrl
                        }

                        Label {
                            text: qsTr("来源原文")
                            color: theme.mutedText
                        }
                        TextArea {
                            objectName: "detailSourceText"
                            Layout.fillWidth: true
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.WrapAnywhere
                            textFormat: Text.PlainText
                            text: controller.sourceText
                        }

                        Label {
                            text: qsTr("原文件地址")
                            color: theme.mutedText
                        }
                        TextArea {
                            objectName: "detailOriginalUrlText"
                            Layout.fillWidth: true
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.WrapAnywhere
                            textFormat: Text.PlainText
                            text: controller.originalFileUrl
                        }

                        Repeater {
                            model: controller.tagsModel.count

                            delegate: ColumnLayout {
                                id: groupDelegate
                                required property int index
                                readonly property int modelRevision:
                                    controller.tagsModel.revision
                                readonly property var groupData: {
                                    const revision = modelRevision
                                    return controller.tagsModel.groupAt(index)
                                }
                                readonly property string groupKey:
                                    groupData.groupKey || ""
                                readonly property string groupLabel:
                                    groupData.groupLabel || ""
                                readonly property var tagItems:
                                    groupData.tagItems || []
                                objectName: "detailTagGroup_" + groupKey
                                Layout.fillWidth: true
                                spacing: 4

                                Label {
                                    text: groupDelegate.groupLabel
                                    color: theme.accentText
                                    font.bold: true
                                }

                                Label {
                                    objectName: "detailEmptyTagGroup_" + groupDelegate.groupKey
                                    visible: groupDelegate.tagItems.length === 0
                                    text: qsTr("暂无标签")
                                    color: theme.subtleText
                                }

                                Flow {
                                    Layout.fillWidth: true
                                    width: parent.width
                                    spacing: 5

                                    Repeater {
                                        model: groupDelegate.tagItems.length

                                        delegate: Button {
                                            id: tagButton
                                            required property int index
                                            readonly property var tagItem:
                                                groupDelegate.tagItems[index]
                                            objectName: "detailTag_" + tagItem.tagName
                                            width: Math.min(implicitWidth, parent.width)
                                            contentItem: Label {
                                                text: tagButton.text
                                                color: theme.text
                                                wrapMode: Text.Wrap
                                                horizontalAlignment: Text.AlignHCenter
                                            }
                                            text: tagItem.chineseNameText === "暂无中文名称"
                                                  ? tagItem.tagName
                                                  : tagItem.tagName + " · "
                                                    + tagItem.chineseNameText
                                            onClicked: tagMenu.open()

                                            Menu {
                                                id: tagMenu
                                                objectName: "detailTagMenu_" + tagButton.tagItem.tagName
                                                y: tagButton.height
                                                property var favoriteAction: null
                                                readonly property var favoriteState: {
                                                    const revision = favorites.revision
                                                    return favorites.tagState(tagButton.tagItem.tagName,
                                                        groupDelegate.groupKey)
                                                }
                                                onAboutToShow: favoriteAction = {
                                                    "key": favorites.postKey,
                                                    "revision": favorites.revision,
                                                    "name": tagButton.tagItem.tagName,
                                                    "category": groupDelegate.groupKey,
                                                    "desired": !favoriteState.saved
                                                }

                                                MenuItem {
                                                    objectName: "detailTagFavorite_" + tagButton.tagItem.tagName
                                                    visible: groupDelegate.groupKey !== "meta"
                                                    text: !tagMenu.favoriteState.known ? qsTr("本地收藏状态未知")
                                                          : tagMenu.favoriteState.saved ? qsTr("取消本地收藏标签")
                                                          : qsTr("本地收藏标签")
                                                    enabled: tagMenu.favoriteState.enabled
                                                    onTriggered: {
                                                        const action = tagMenu.favoriteAction
                                                        if (action)
                                                            favorites.setTagFavorite(action.key, action.revision,
                                                                action.name, action.category, action.desired)
                                                    }
                                                }

                                                MenuItem {
                                                    objectName: "detailTagNewQuery_"
                                                                + tagButton.tagItem.tagName
                                                    text: qsTr("以此标签新建查询")
                                                    onTriggered: controller.activateTag(
                                                        tagButton.tagItem.tagName
                                                    )
                                                }

                                                MenuItem {
                                                    objectName: "detailTagAddToQuery_"
                                                                + tagButton.tagItem.tagName
                                                    text: qsTr("加入当前复杂查询")
                                                    visible: page.complexQueryActionsEnabled
                                                    onTriggered: page.addTagToQueryRequested(
                                                        tagButton.tagItem.tagName
                                                    )
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        Item {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 8
                        }
                    }
                }
            }
        }
    }
}
