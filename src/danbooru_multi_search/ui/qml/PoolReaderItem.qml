import QtQuick
import QtQuick.Controls

Item {
    id: pageItem
    required property var controller
    required property int pageNumber
    required property real readingWidth
    required property real viewportHeight
    property bool single: false
    property QtObject theme: AppTheme {}
    readonly property var row: { const revision = controller.model.revision; return controller.pageAt(pageNumber) }
    readonly property var media: { const revision = controller.revision; return controller.mediaFor(pageNumber) }
    readonly property real fittedWidth: controller.scaleMode === "fit-window"
        ? Math.min(readingWidth, viewportHeight / (row.aspectRatio || 1.4)) : readingWidth
    readonly property real pictureWidth: fittedWidth * controller.zoom
    readonly property real pictureHeight: pictureWidth * (row.aspectRatio || 1.4)
    property int rebuildToken: 0
    property int syncToken: 0
    property bool applyingScale: false
    property var loadedMedia: null
    property int loadedGeneration: -1
    property int loadedAttempt: -1
    property string loadedUrl: ""
    objectName: "readerPage_" + pageNumber
    height: single ? viewportHeight : Math.max(180, pictureHeight + 36)

    function requestSync() {
        const token = ++syncToken
        Qt.callLater(function() {
            if (token !== syncToken) return
            syncMedia()
            if (single) applyScale()
        })
    }

    function syncMedia() {
        if (!picture || !singleViewer || !controller) return
        const allowed = controller.active && media && media.mediaUrl.length
            && controller.mode === (single ? "single" : "continuous")
        if (single) {
            if (singleViewer.item && allowed)
                singleViewer.item.loadCandidate(media.mediaUrl, media.mediaComponent,
                    media.mediaGeneration, media.candidateAttempt)
            else if (singleViewer.item)
                singleViewer.item.clearMedia()
        } else if (!allowed) {
            ++rebuildToken
            picture.active = false
            loadedMedia = null
        } else {
            if (loadedMedia === media && loadedGeneration === media.mediaGeneration
                    && loadedAttempt === media.candidateAttempt && loadedUrl === media.mediaUrl)
                return
            ++rebuildToken
            picture.active = false
            loadedMedia = media
            loadedGeneration = media.mediaGeneration
            loadedAttempt = media.candidateAttempt
            loadedUrl = media.mediaUrl
            picture.sourceComponent = media.mediaComponent === "animated" ? animated : still
            const token = rebuildToken
            Qt.callLater(function() {
                if (token === rebuildToken && controller.active && pageItem.media
                        && controller.mode === "continuous")
                    picture.active = true
            })
        }
    }

    function applyScale() {
        const viewer = singleViewer.item
        if (!viewer || !viewer.intrinsicWidth) return
        applyingScale = true
        if (controller.scaleMode === "fit-window") viewer.fitWindow()
        else if (controller.scaleMode === "fit-width") viewer.fitWidth()
        else viewer.setScaleAt(viewer.width / viewer.intrinsicWidth * controller.zoom,
                               viewer.width / 2, viewer.height / 2, "manual")
        applyingScale = false
    }

    function initialize(image) {
        image.frozenMedia = media
        image.frozenGeneration = media.mediaGeneration
        image.frozenAttempt = media.candidateAttempt
        image.frozenUrl = media.mediaUrl
        image.frozenCache = imageCacheController.cacheGeneration
        image.source = image.frozenUrl
    }

    function report(image) {
        if (!controller.active || !media || media !== image.frozenMedia
                || media.mediaGeneration !== image.frozenGeneration
                || media.candidateAttempt !== image.frozenAttempt
                || media.mediaUrl !== image.frozenUrl) return
        if (image.status === Image.Ready) {
            const action = imageCacheController.decodeSuccessActionForGeneration(image.frozenUrl, image.frozenCache)
            if (action === "accepted") media.reportMediaSuccess(image.frozenGeneration, image.frozenAttempt, image.frozenUrl)
            else if (action === "stopped") media.reportMediaStopped(image.frozenGeneration, image.frozenAttempt, image.frozenUrl)
        } else if (image.status === Image.Error) {
            const action = imageCacheController.decodeFailureActionForGeneration(image.frozenUrl,
                imageCacheController.finalUrlFor(image.frozenUrl), image.frozenCache)
            if (action === "retry") { loadedMedia = null; requestSync() }
            else if (action === "failed") media.reportMediaFailure(image.frozenGeneration, image.frozenAttempt, image.frozenUrl)
            else if (action === "stopped") media.reportMediaStopped(image.frozenGeneration, image.frozenAttempt, image.frozenUrl)
        }
    }

    onMediaChanged: requestSync()
    Component.onCompleted: requestSync()
    Component.onDestruction: { ++syncToken; ++rebuildToken; picture.active = false }
    Connections { target: pageItem.media; function onMediaChanged() { pageItem.requestSync() } }
    Connections {
        target: controller
        function onChanged() { pageItem.requestSync() }
    }
    Rectangle { anchors.fill: parent; color: theme.viewer }
    Label {
        anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter
        text: qsTr("第 %1 页 · #%2").arg(pageNumber).arg(row.postKey || "")
        color: theme.secondaryText; height: single ? 0 : 32; visible: !single
    }
    Loader {
        id: picture
        objectName: "readerContinuousMedia_" + pageNumber
        active: false
        y: 36; width: pageItem.pictureWidth; height: pageItem.pictureHeight
        x: Math.max(0, (pageItem.width - width) / 2)
    }
    Loader {
        id: singleViewer
        anchors.fill: parent
        active: pageItem.single && controller.active && controller.mode === "single" && Boolean(pageItem.media)
        sourceComponent: ImageViewer {
            theme: pageItem.theme
            Component.onCompleted: pageItem.requestSync()
            onCandidateSucceeded: function(generation, attempt, url, pixelWidth, pixelHeight) {
                if (pageItem.media && pageItem.media.reportMediaSuccess(generation, attempt, url)) {
                    acceptDecodedDimensions(generation, attempt, url, pixelWidth, pixelHeight)
                    pageItem.applyScale()
                }
            }
            onCandidateFailed: function(generation, attempt, url) {
                if (pageItem.media) pageItem.media.reportMediaFailure(generation, attempt, url)
            }
            onCandidateStopped: function(generation, attempt, url) {
                if (pageItem.media) pageItem.media.reportMediaStopped(generation, attempt, url)
            }
            onZoomScaleChanged: {
                if (!pageItem.applyingScale && scaleMode === "manual" && intrinsicWidth > 0
                        && pageNumber === controller.currentPage) {
                    const factor = zoomScale * intrinsicWidth / width
                    if (Math.abs(factor - controller.zoom) > 0.001)
                        controller.setZoom(factor, controller.revision)
                }
            }
        }
    }
    Label {
        anchors.centerIn: parent
        objectName: "readerPageNotice_" + pageNumber
        width: Math.min(parent.width - 32, 600)
        visible: !pageItem.media || !pageItem.media.mediaUrl.length || pageItem.media.restrictedFormat
                 || pageItem.media.mediaState === "error" || pageItem.media.mediaState === "stopped"
        text: row.pageState === "available"
            ? (pageItem.media ? (pageItem.media.formatStatusText.length
                ? pageItem.media.formatStatusText + qsTr("；可下载原文件查看。") : pageItem.media.mediaMessage)
                : qsTr("在当前页附近加载图片。"))
            : row.pageMessage || qsTr("正在读取页面……")
        wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter; color: theme.secondaryText
    }
    Component {
        id: still
        Image {
            property var frozenMedia: null
            property int frozenGeneration: 0
            property int frozenAttempt: 0
            property int frozenCache: 0
            property string frozenUrl: ""
            asynchronous: true; fillMode: Image.PreserveAspectFit
            Component.onCompleted: pageItem.initialize(this)
            onStatusChanged: pageItem.report(this)
        }
    }
    Component {
        id: animated
        AnimatedImage {
            property var frozenMedia: null
            property int frozenGeneration: 0
            property int frozenAttempt: 0
            property int frozenCache: 0
            property string frozenUrl: ""
            asynchronous: true; fillMode: Image.PreserveAspectFit
            playing: pageItem.visible && controller.active
            Component.onCompleted: pageItem.initialize(this)
            onStatusChanged: pageItem.report(this)
        }
    }
}
