import QtQuick
import QtQuick.Effects

Rectangle {
    id: background
    objectName: "applicationBackground"
    required property var controller
    property QtObject theme: AppTheme {}
    readonly property bool imageReady: backgroundLoader.item !== null
                                       && backgroundLoader.item.status === Image.Ready
    readonly property bool effectsAvailable: GraphicsInfo.api !== GraphicsInfo.Software
    color: theme.background
    clip: true

    function rebuildImage() {
        backgroundLoader.active = false
        const generation = controller.backgroundGeneration
        Qt.callLater(function() {
            if (generation === controller.backgroundGeneration)
                backgroundLoader.active = controller.backgroundUrl.length > 0
        })
    }

    Component.onCompleted: rebuildImage()
    Connections {
        target: background.controller
        function onBackgroundChanged() { background.rebuildImage() }
    }

    Loader {
        id: backgroundLoader
        objectName: "backgroundLoader"
        anchors.fill: parent
        active: false
        sourceComponent: Image {
            id: image
            objectName: "backgroundImage"
            property int frozenGeneration: 0
            property string frozenUrl: ""
            source: ""
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            cache: false
            visible: !background.effectsAvailable && status === Image.Ready
            Component.onCompleted: {
                frozenGeneration = background.controller.backgroundGeneration
                frozenUrl = background.controller.backgroundUrl
                source = frozenUrl
            }
            onStatusChanged: {
                if (status === Image.Ready || status === Image.Error)
                    background.controller.reportBackgroundResult(
                        frozenGeneration, frozenUrl, status === Image.Ready)
            }
        }
    }

    MultiEffect {
        objectName: "backgroundEffect"
        anchors.fill: parent
        source: backgroundLoader.item
        visible: background.imageReady && background.effectsAvailable
        autoPaddingEnabled: false
        blurEnabled: background.controller.backgroundBlur > 0
        blurMax: 64
        blur: background.controller.backgroundBlur
        brightness: background.controller.backgroundBrightness - 1.0
    }

    Rectangle {
        anchors.fill: parent
        color: background.theme.background
        opacity: background.controller.backgroundMask
        visible: background.imageReady
    }
}
