import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Popup {
    id: panel
    objectName: "appearancePanel"
    required property var controller
    property QtObject theme: AppTheme {}
    parent: Overlay.overlay
    width: Math.min(480, parent ? parent.width - 32 : 480)
    height: Math.min(contentColumn.implicitHeight + 48,
                     parent ? parent.height - 32 : 560)
    anchors.centerIn: parent
    modal: true
    focus: true
    padding: 24
    font.pixelSize: theme.fontSize
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

    function saveAdjustments() {
        controller.setBackgroundAdjustments(maskSlider.value, blurSlider.value,
                                            brightnessSlider.value)
    }

    background: Rectangle {
        color: panel.theme.surface
        border.color: panel.theme.borderStrong
        radius: panel.theme.radius
    }

    contentItem: Flickable {
        clip: true
        contentHeight: contentColumn.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}

        ColumnLayout {
            id: contentColumn
            width: parent.width
            spacing: 20

            RowLayout {
                Layout.fillWidth: true
                Label {
                    text: qsTr("外观")
                    font.pixelSize: 22
                    font.bold: true
                    color: panel.theme.text
                    Layout.fillWidth: true
                }
                ActionButton { text: qsTr("关闭"); flat: true; onClicked: panel.close() }
            }
            Label {
                text: qsTr("主题")
                color: panel.theme.text
                font.pixelSize: 16
                font.bold: true
            }
            ComboBox {
                id: themeChoice
                objectName: "themeChoice"
                Layout.fillWidth: true
                model: [qsTr("跟随系统"), qsTr("浅色"), qsTr("深色")]
                currentIndex: ["system", "light", "dark"].indexOf(controller.themeMode)
                onActivated: controller.setTheme(["system", "light", "dark"][currentIndex])
            }
            Label {
                text: qsTr("应用背景")
                color: panel.theme.text
                font.pixelSize: 16
                font.bold: true
            }
            RowLayout {
                Layout.fillWidth: true
                ActionButton {
                    objectName: "chooseBackgroundButton"
                    text: qsTr("选择本地图片")
                    onClicked: backgroundDialog.open()
                }
                ActionButton {
                    objectName: "clearBackgroundButton"
                    text: qsTr("恢复默认")
                    enabled: controller.backgroundPath.length > 0
                    onClicked: controller.clearBackground()
                }
            }
            Label {
                objectName: "backgroundPathLabel"
                Layout.fillWidth: true
                text: controller.backgroundPath || qsTr("当前使用默认背景")
                color: panel.theme.mutedText
                wrapMode: Text.WrapAnywhere
                font.pixelSize: 12
            }
            Label {
                text: qsTr("背景遮罩 · %1%").arg(Math.round(maskSlider.value * 100))
                color: panel.theme.secondaryText
            }
            Slider {
                id: maskSlider
                objectName: "backgroundMaskSlider"
                Layout.fillWidth: true
                from: 0; to: 1
                value: controller.backgroundMask
                onPressedChanged: { if (!pressed) panel.saveAdjustments() }
                onMoved: { if (!pressed) panel.saveAdjustments() }
            }
            Label {
                text: qsTr("模糊程度 · %1%").arg(Math.round(blurSlider.value * 100))
                color: panel.theme.secondaryText
            }
            Slider {
                id: blurSlider
                objectName: "backgroundBlurSlider"
                Layout.fillWidth: true
                from: 0; to: 1
                value: controller.backgroundBlur
                onPressedChanged: { if (!pressed) panel.saveAdjustments() }
                onMoved: { if (!pressed) panel.saveAdjustments() }
            }
            Label {
                text: qsTr("亮度 · %1%").arg(Math.round(brightnessSlider.value * 100))
                color: panel.theme.secondaryText
            }
            Slider {
                id: brightnessSlider
                objectName: "backgroundBrightnessSlider"
                Layout.fillWidth: true
                from: 0; to: 2
                value: controller.backgroundBrightness
                onPressedChanged: { if (!pressed) panel.saveAdjustments() }
                onMoved: { if (!pressed) panel.saveAdjustments() }
            }
            Label {
                objectName: "appearanceStatus"
                Layout.fillWidth: true
                visible: text.length > 0
                text: controller.statusText
                color: panel.theme.warning
                wrapMode: Text.Wrap
            }
        }
    }

    FileDialog {
        id: backgroundDialog
        objectName: "backgroundDialog"
        title: qsTr("选择应用背景")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("图片 (*.jpg *.jpeg *.png *.webp *.avif *.bmp)"),
                      qsTr("所有文件 (*)")]
        onAccepted: panel.controller.selectBackground(selectedFile)
    }
}
