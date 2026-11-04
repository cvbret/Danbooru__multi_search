import QtQuick
import QtQuick.Controls

Item {
    id: viewer
    objectName: "detailImageViewer"

    property string candidateUrl: ""
    property string componentType: "none"
    property int candidateGeneration: 0
    property int candidateAttempt: 0
    property int mediaRebuildToken: 0
    property real intrinsicWidth: 0
    property real intrinsicHeight: 0
    property real zoomScale: 1.0
    property string scaleMode: "fit-window"
    readonly property real renderedWidth: intrinsicWidth * zoomScale
    readonly property real renderedHeight: intrinsicHeight * zoomScale
    readonly property real imageOffsetX: Math.max((viewport.width - renderedWidth) / 2, 0)
    readonly property real imageOffsetY: Math.max((viewport.height - renderedHeight) / 2, 0)

    signal candidateSucceeded(int generation, int attempt, string url,
                              real pixelWidth, real pixelHeight)
    signal candidateFailed(int generation, int attempt, string url)
    signal candidateStopped(int generation, int attempt, string url)

    function loadCandidate(url, component, generation, attempt) {
        if (candidateUrl === url
                && componentType === component
                && candidateGeneration === generation
                && candidateAttempt === attempt)
            return
        ++mediaRebuildToken
        mediaLoader.active = false
        candidateUrl = url
        componentType = component
        candidateGeneration = generation
        candidateAttempt = attempt
        intrinsicWidth = 0
        intrinsicHeight = 0
        zoomScale = 1.0
        scaleMode = "fit-window"
        viewport.contentX = 0
        viewport.contentY = 0
        mediaLoader.sourceComponent = component === "animated"
                                      ? animatedMedia
                                      : staticMedia
        mediaLoader.active = url.length > 0 && component !== "none"
    }

    function clearMedia() {
        ++mediaRebuildToken
        mediaLoader.active = false
        candidateUrl = ""
        componentType = "none"
        intrinsicWidth = 0
        intrinsicHeight = 0
        zoomScale = 1.0
        scaleMode = "fit-window"
        viewport.contentX = 0
        viewport.contentY = 0
    }

    function retryDecodedCandidate(generation, attempt, url) {
        if (generation !== candidateGeneration
                || attempt !== candidateAttempt
                || url !== candidateUrl)
            return false
        const token = ++mediaRebuildToken
        mediaLoader.active = false
        Qt.callLater(function() {
            if (token !== mediaRebuildToken
                    || generation !== candidateGeneration
                    || attempt !== candidateAttempt
                    || url !== candidateUrl)
                return
            mediaLoader.active = true
        })
        return true
    }

    function handleDecodeFailure(generation, attempt, url, cacheGeneration) {
        if (generation !== candidateGeneration
                || attempt !== candidateAttempt
                || url !== candidateUrl)
            return
        const finalUrl = imageCacheController.finalUrlFor(url)
        const action = imageCacheController.decodeFailureActionForGeneration(
            url,
            finalUrl,
            cacheGeneration
        )
        if (action === "retry") {
            retryDecodedCandidate(generation, attempt, url)
            return
        }
        if (action === "stopped")
            stopCandidate(generation, attempt, url)
        else if (action === "failed")
            candidateFailed(generation, attempt, url)
    }

    function stopCandidate(generation, attempt, url) {
        if (generation !== candidateGeneration
                || attempt !== candidateAttempt
                || url !== candidateUrl)
            return
        candidateStopped(generation, attempt, url)
        ++mediaRebuildToken
        mediaLoader.active = false
    }

    function acceptDecodedDimensions(generation, attempt, url, pixelWidth, pixelHeight) {
        if (generation !== candidateGeneration
                || attempt !== candidateAttempt
                || url !== candidateUrl
                || pixelWidth <= 0 || pixelHeight <= 0)
            return false
        intrinsicWidth = pixelWidth
        intrinsicHeight = pixelHeight
        fitWindow()
        return true
    }

    function canScale() {
        return viewport.width > 0 && viewport.height > 0
                && intrinsicWidth > 0 && intrinsicHeight > 0
    }

    function fitWindow() {
        if (!canScale())
            return false
        scaleMode = "fit-window"
        zoomScale = Math.min(viewport.width / intrinsicWidth,
                             viewport.height / intrinsicHeight)
        viewport.contentX = 0
        viewport.contentY = 0
        return true
    }

    function fitWidth() {
        if (!canScale())
            return false
        scaleMode = "fit-width"
        zoomScale = viewport.width / intrinsicWidth
        viewport.contentX = 0
        viewport.contentY = 0
        return true
    }

    function actualSize() {
        if (!canScale())
            return false
        return setScaleAt(1.0, viewport.width / 2, viewport.height / 2, "actual")
    }

    function zoomAt(factor, pointX, pointY) {
        if (!canScale() || factor <= 0)
            return false
        const nextScale = Math.max(0.05, Math.min(8.0, zoomScale * factor))
        return setScaleAt(nextScale, pointX, pointY, "manual")
    }

    function setScaleAt(nextScale, pointX, pointY, nextMode) {
        if (!canScale() || nextScale <= 0)
            return false
        const oldScale = zoomScale
        const oldOffsetX = imageOffsetX
        const oldOffsetY = imageOffsetY
        const imagePixelX = (viewport.contentX + pointX - oldOffsetX) / oldScale
        const imagePixelY = (viewport.contentY + pointY - oldOffsetY) / oldScale
        scaleMode = nextMode
        zoomScale = nextScale
        const nextOffsetX = Math.max(
            (viewport.width - intrinsicWidth * nextScale) / 2,
            0
        )
        const nextOffsetY = Math.max(
            (viewport.height - intrinsicHeight * nextScale) / 2,
            0
        )
        viewport.contentX = nextOffsetX + imagePixelX * nextScale - pointX
        viewport.contentY = nextOffsetY + imagePixelY * nextScale - pointY
        clampOffsets()
        return true
    }

    function clampOffsets() {
        const maximumX = Math.max(renderedWidth - viewport.width, 0)
        const maximumY = Math.max(renderedHeight - viewport.height, 0)
        viewport.contentX = Math.max(0, Math.min(viewport.contentX, maximumX))
        viewport.contentY = Math.max(0, Math.min(viewport.contentY, maximumY))
    }

    function viewportChanged() {
        if (!canScale())
            return
        if (scaleMode === "fit-window")
            fitWindow()
        else if (scaleMode === "fit-width")
            fitWidth()
        else if (scaleMode === "actual") {
            zoomScale = 1.0
            clampOffsets()
        } else {
            clampOffsets()
        }
    }

    Rectangle {
        anchors.fill: parent
        color: "#0b0d11"
        radius: 6

        Flickable {
            id: viewport
            objectName: "detailImageViewport"
            anchors.fill: parent
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            contentWidth: Math.max(width, viewer.renderedWidth)
            contentHeight: Math.max(height, viewer.renderedHeight)
            onWidthChanged: viewer.viewportChanged()
            onHeightChanged: viewer.viewportChanged()

            Loader {
                id: mediaLoader
                objectName: "detailMediaLoader"
                active: false
                asynchronous: false
                x: viewer.imageOffsetX
                y: viewer.imageOffsetY
                width: viewer.renderedWidth
                height: viewer.renderedHeight
            }
        }

        Item {
            anchors.fill: parent
            z: 1

            WheelHandler {
                target: null
                blocking: true
                onWheel: function(event) {
                    event.accepted = true
                    if (event.phase !== Qt.ScrollBegin
                            && event.phase !== Qt.ScrollEnd
                            && event.angleDelta.y !== 0) {
                        viewer.zoomAt(
                            event.angleDelta.y > 0 ? 1.15 : 1 / 1.15,
                            event.x,
                            event.y
                        )
                    }
                }
            }
        }
    }

    Component {
        id: staticMedia

        Image {
            id: staticImage
            objectName: "detailStaticImage"
            source: ""
            cache: false
            asynchronous: true
            fillMode: Image.Stretch

            property int frozenGeneration: 0
            property int frozenAttempt: 0
            property int frozenCacheGeneration: 0
            property string frozenUrl: ""

            Component.onCompleted: {
                frozenGeneration = viewer.candidateGeneration
                frozenAttempt = viewer.candidateAttempt
                frozenCacheGeneration = imageCacheController.cacheGeneration
                frozenUrl = viewer.candidateUrl
                source = frozenUrl
            }

            onStatusChanged: {
                if (status === Image.Ready) {
                    const action = imageCacheController.decodeSuccessActionForGeneration(
                        frozenUrl,
                        frozenCacheGeneration
                    )
                    if (action === "accepted") {
                        viewer.candidateSucceeded(
                            frozenGeneration,
                            frozenAttempt,
                            frozenUrl,
                            sourceSize.width > 0 ? sourceSize.width : implicitWidth,
                            sourceSize.height > 0 ? sourceSize.height : implicitHeight
                        )
                    } else if (action === "stopped") {
                        viewer.stopCandidate(
                            frozenGeneration,
                            frozenAttempt,
                            frozenUrl
                        )
                    }
                } else if (status === Image.Error) {
                    viewer.handleDecodeFailure(
                        frozenGeneration,
                        frozenAttempt,
                        frozenUrl,
                        frozenCacheGeneration
                    )
                }
            }
        }
    }

    Component {
        id: animatedMedia

        AnimatedImage {
            id: animatedImage
            objectName: "detailAnimatedImage"
            source: ""
            cache: false
            asynchronous: true
            fillMode: Image.Stretch
            playing: status === Image.Ready && viewer.visible

            property int frozenGeneration: 0
            property int frozenAttempt: 0
            property int frozenCacheGeneration: 0
            property string frozenUrl: ""

            Component.onCompleted: {
                frozenGeneration = viewer.candidateGeneration
                frozenAttempt = viewer.candidateAttempt
                frozenCacheGeneration = imageCacheController.cacheGeneration
                frozenUrl = viewer.candidateUrl
                source = frozenUrl
            }

            onStatusChanged: {
                if (status === Image.Ready) {
                    const action = imageCacheController.decodeSuccessActionForGeneration(
                        frozenUrl,
                        frozenCacheGeneration
                    )
                    if (action === "accepted") {
                        viewer.candidateSucceeded(
                            frozenGeneration,
                            frozenAttempt,
                            frozenUrl,
                            sourceSize.width > 0 ? sourceSize.width : implicitWidth,
                            sourceSize.height > 0 ? sourceSize.height : implicitHeight
                        )
                    } else if (action === "stopped") {
                        viewer.stopCandidate(
                            frozenGeneration,
                            frozenAttempt,
                            frozenUrl
                        )
                    }
                } else if (status === Image.Error) {
                    viewer.handleDecodeFailure(
                        frozenGeneration,
                        frozenAttempt,
                        frozenUrl,
                        frozenCacheGeneration
                    )
                }
            }
        }
    }
}
