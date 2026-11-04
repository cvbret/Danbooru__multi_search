import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: page
    objectName: "detailPage"

    required property var controller
    signal backRequested()
    signal importTagsRequested()

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

    Connections {
        target: controller

        function onMediaChanged() {
            page.syncMedia()
        }
    }

    Component.onCompleted: syncMedia()

    ColumnLayout {
        anchors.fill: parent
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Button {
                objectName: "detailBackButton"
                text: qsTr("返回画廊")
                onClicked: page.backRequested()
            }

            Button {
                objectName: "detailImportTagsButton"
                text: qsTr("导入标签库")
                onClicked: page.importTagsRequested()
            }

            Label {
                text: qsTr("作品 #%1").arg(controller.postIdText)
                color: "#f4f4f5"
                font.bold: true
                font.pixelSize: 18
            }

            Item { Layout.fillWidth: true }

            Button {
                objectName: "detailPreviewButton"
                text: qsTr("预览图")
                checkable: true
                checked: controller.requestedQuality === "preview"
                onClicked: controller.requestQuality("preview")
            }

            Button {
                objectName: "detailLargeButton"
                text: qsTr("较大图片")
                checkable: true
                checked: controller.requestedQuality === "large"
                onClicked: controller.requestQuality("large")
            }

            Button {
                objectName: "detailOriginalButton"
                text: qsTr("原图")
                checkable: true
                checked: controller.requestedQuality === "original"
                onClicked: controller.requestQuality("original")
            }

            ToolSeparator {}

            Button {
                objectName: "detailFitWindowButton"
                text: qsTr("适应窗口")
                onClicked: imageViewer.fitWindow()
            }

            Button {
                objectName: "detailFitWidthButton"
                text: qsTr("适应宽度")
                onClicked: imageViewer.fitWidth()
            }

            Button {
                objectName: "detailActualSizeButton"
                text: "100%"
                onClicked: imageViewer.actualSize()
            }

            Button {
                objectName: "detailZoomOutButton"
                text: "−"
                onClicked: imageViewer.zoomAt(
                    1 / 1.15,
                    imageViewer.width / 2,
                    imageViewer.height / 2
                )
            }

            Button {
                objectName: "detailZoomInButton"
                text: "+"
                onClicked: imageViewer.zoomAt(
                    1.15,
                    imageViewer.width / 2,
                    imageViewer.height / 2
                )
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 6

                ImageViewer {
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
                        color: controller.mediaState === "error" ? "#fda4af" : "#d4d4d8"
                        wrapMode: Text.Wrap
                    }

                    Label {
                        text: qsTr("请求：%1 · 已加载：%2 · %3%")
                              .arg(controller.requestedQualityText)
                              .arg(controller.actualQualityText)
                              .arg((imageViewer.zoomScale * 100).toFixed(1))
                        color: "#a1a1aa"
                    }

                    Button {
                        objectName: "detailRetryMediaButton"
                        visible: controller.canRetryMedia
                        text: qsTr("重试图片")
                        onClicked: controller.retryMedia()
                    }
                }
            }

            Rectangle {
                Layout.preferredWidth: Math.min(390, page.width * 0.36)
                Layout.minimumWidth: 300
                Layout.fillHeight: true
                color: "#191c23"
                border.color: "#343944"
                radius: 7

                ScrollView {
                    anchors.fill: parent
                    anchors.margins: 12
                    clip: true

                    ColumnLayout {
                        width: Math.max(260, parent.width - 8)
                        spacing: 9

                        Label {
                            text: qsTr("Danbooru 收录时间")
                            color: "#a1a1aa"
                        }
                        Label {
                            Layout.fillWidth: true
                            text: controller.createdAtText
                            color: "#f4f4f5"
                            wrapMode: Text.Wrap
                        }

                        Label {
                            text: qsTr("尺寸与文件")
                            color: "#a1a1aa"
                        }
                        Label {
                            Layout.fillWidth: true
                            text: qsTr("%1 · %2 · %3（%4）")
                                  .arg(controller.dimensionsText)
                                  .arg(controller.fileExtensionText)
                                  .arg(controller.fileSizeBytesText)
                                  .arg(controller.fileSizeReadableText)
                            color: "#f4f4f5"
                            wrapMode: Text.Wrap
                        }

                        Label {
                            objectName: "detailFormatStatus"
                            Layout.fillWidth: true
                            visible: controller.formatStatusText.length > 0
                            text: controller.formatStatusText
                            color: controller.formatState === "unknown"
                                   ? "#fbbf24"
                                   : "#fda4af"
                            wrapMode: Text.Wrap
                        }

                        Label {
                            text: qsTr("内容与人气")
                            color: "#a1a1aa"
                        }
                        Label {
                            Layout.fillWidth: true
                            text: qsTr("%1 · 得分 %2 · 收藏 %3")
                                  .arg(controller.ratingText)
                                  .arg(controller.scoreText)
                                  .arg(controller.favoriteText)
                            color: "#f4f4f5"
                            wrapMode: Text.Wrap
                        }

                        Label {
                            text: qsTr("Danbooru 地址")
                            color: "#a1a1aa"
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
                            color: "#a1a1aa"
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
                            color: "#a1a1aa"
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
                                    color: "#93c5fd"
                                    font.bold: true
                                }

                                Label {
                                    objectName: "detailEmptyTagGroup_" + groupDelegate.groupKey
                                    visible: groupDelegate.tagItems.length === 0
                                    text: qsTr("暂无标签")
                                    color: "#71717a"
                                }

                                Flow {
                                    Layout.fillWidth: true
                                    width: parent.width
                                    spacing: 5

                                    Repeater {
                                        model: groupDelegate.tagItems.length

                                        delegate: Button {
                                            required property int index
                                            readonly property var tagItem:
                                                groupDelegate.tagItems[index]
                                            objectName: "detailTag_" + tagItem.tagName
                                            text: tagItem.chineseNameText === "暂无中文名称"
                                                  ? tagItem.tagName
                                                  : tagItem.tagName + " · "
                                                    + tagItem.chineseNameText
                                            onClicked: controller.activateTag(tagItem.tagName)
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
