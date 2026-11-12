import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "favoriteTransferDialog"
    required property var controller
    property QtObject theme: AppTheme {}
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(660, parent ? parent.width - 32 : 660)
    height: Math.min(720, parent ? parent.height - 32 : 720)
    modal: true
    title: qsTr("导入与导出本地收藏")
    closePolicy: controller.busy ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    onClosed: { if (!controller.busy) controller.cancel() }

    FileDialog {
        id: importFile
        objectName: "favoriteImportFileDialog"
        property string choiceToken: ""
        title: qsTr("选择本地收藏 JSON")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("JSON 文件 (*.json)")]
        onAccepted: dialog.controller.acceptImportFile(selectedFile, choiceToken)
        onRejected: dialog.controller.cancelSelection(choiceToken)
    }
    FileDialog {
        id: exportFile
        objectName: "favoriteExportFileDialog"
        property string choiceToken: ""
        title: qsTr("导出本地收藏 JSON")
        fileMode: FileDialog.SaveFile
        defaultSuffix: "json"
        nameFilters: [qsTr("JSON 文件 (*.json)")]
        onAccepted: dialog.controller.acceptExportFile(selectedFile, choiceToken)
        onRejected: dialog.controller.cancelSelection(choiceToken)
    }

    contentItem: ScrollView {
        objectName: "favoriteTransferScroll"
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: parent.width
            spacing: 16
            Label {
                Layout.fillWidth: true
                text: qsTr("JSON 版本 1 只包含收藏与文件夹，不包含应用设置、缓存或画师组合。")
                color: theme.secondaryText
                wrapMode: Text.Wrap
            }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ActionButton {
                    objectName: "chooseFavoriteImportFileButton"
                    text: qsTr("选择导入文件")
                    enabled: !dialog.controller.busy
                    onClicked: {
                        importFile.choiceToken = dialog.controller.beginImportSelection()
                        if (importFile.choiceToken.length)
                            importFile.open()
                    }
                }
                ActionButton {
                    objectName: "chooseFavoriteExportFileButton"
                    text: qsTr("导出到文件")
                    enabled: !dialog.controller.busy
                    onClicked: {
                        exportFile.choiceToken = dialog.controller.beginExportSelection()
                        if (exportFile.choiceToken.length)
                            exportFile.open()
                    }
                }
                ComboBox {
                    objectName: "favoriteImportMode"
                    width: 150
                    enabled: !dialog.controller.busy
                    textRole: "label"; valueRole: "value"
                    model: [{"value":"merge","label":qsTr("合并导入")},{"value":"replace","label":qsTr("覆盖导入")}]
                    currentIndex: dialog.controller.importMode === "replace" ? 1 : 0
                    onActivated: dialog.controller.setImportMode(currentValue)
                }
                ActionButton {
                    objectName: "previewFavoriteImportAgainButton"
                    text: qsTr("重新预览")
                    enabled: !dialog.controller.busy && dialog.controller.sourcePath.length > 0
                    onClicked: dialog.controller.previewAgain()
                }
            }
            Label {
                objectName: "favoriteImportSourcePath"
                Layout.fillWidth: true
                text: dialog.controller.sourcePath.length ? dialog.controller.sourcePath : qsTr("尚未选择导入文件")
                wrapMode: Text.WrapAnywhere
                color: theme.mutedText
            }
            Label {
                Layout.fillWidth: true
                text: dialog.controller.importMode === "replace"
                      ? qsTr("覆盖会替换现有收藏和文件夹。空文档也能清空本地收藏，请核对删除数量。")
                      : qsTr("合并只新增缺少的收藏和文件夹，已有记录保留原文件夹、首次时间与概要。")
                wrapMode: Text.Wrap
                color: dialog.controller.importMode === "replace" ? theme.error : theme.secondaryText
            }
            BusyIndicator { running: dialog.controller.busy; visible: running; Layout.alignment: Qt.AlignHCenter }
            Label {
                objectName: "favoriteTransferMessage"
                Layout.fillWidth: true
                text: dialog.controller.message
                wrapMode: Text.Wrap
                color: dialog.controller.errorCode.length ? theme.error : theme.text
            }
            Label {
                objectName: "favoriteTransferErrorCode"
                Layout.fillWidth: true
                visible: dialog.controller.errorCode.length > 0
                text: dialog.controller.errorCode
                wrapMode: Text.WrapAnywhere
                color: theme.error
            }
            Label {
                objectName: "favoriteTransferResidualPath"
                Layout.fillWidth: true
                visible: dialog.controller.residualPath.length > 0
                text: qsTr("保留的临时文件：%1").arg(dialog.controller.residualPath)
                wrapMode: Text.WrapAnywhere
                color: theme.error
            }
            GridLayout {
                objectName: "favoriteImportPreviewCounts"
                Layout.fillWidth: true
                columns: dialog.width < 500 ? 1 : 2
                rowSpacing: 10
                columnSpacing: 24
                visible: dialog.controller.preview.token.length > 0
                Label { objectName: "previewAddedFolders"; text: qsTr("新增文件夹：%1").arg(dialog.controller.preview.addedFolders || 0); color: theme.text }
                Label { objectName: "previewReusedFolders"; text: qsTr("复用文件夹：%1").arg(dialog.controller.preview.reusedFolders || 0); color: theme.text }
                Label { objectName: "previewAddedFavorites"; text: qsTr("新增收藏：%1").arg(dialog.controller.preview.addedFavorites || 0); color: theme.text }
                Label { objectName: "previewPreservedFavorites"; text: qsTr("保留收藏：%1").arg(dialog.controller.preview.preservedFavorites || 0); color: theme.text }
                Label { objectName: "previewDeletedFolders"; text: qsTr("删除文件夹：%1").arg(dialog.controller.preview.deletedFolders || 0); color: theme.error }
                Label { objectName: "previewDeletedFavorites"; text: qsTr("删除收藏：%1").arg(dialog.controller.preview.deletedFavorites || 0); color: theme.error }
            }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                ActionButton {
                    objectName: "cancelFavoriteTransferButton"
                    text: qsTr("取消预览或传输")
                    enabled: dialog.controller.canCancel
                    onClicked: dialog.controller.cancel()
                }
                ActionButton {
                    objectName: "confirmFavoriteImportButton"
                    text: dialog.controller.importMode === "replace" ? qsTr("确认覆盖导入") : qsTr("确认合并导入")
                    primary: true
                    enabled: dialog.controller.canConfirm
                    property string frozenToken: ""
                    onPressed: frozenToken = dialog.controller.preview.token
                    onClicked: dialog.controller.confirmImport(frozenToken)
                }
                ActionButton {
                    objectName: "closeFavoriteTransferButton"
                    text: qsTr("关闭")
                    enabled: !dialog.controller.busy
                    onClicked: dialog.close()
                }
            }
        }
    }
}
