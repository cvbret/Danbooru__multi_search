import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: card
    objectName: "postCard_" + postKey
    required property string postKey
    required property string selectionKey
    required property int actionRevision
    required property bool selected
    required property bool favoriteKnown
    required property bool favoriteSaved
    required property bool favoriteEnabled
    property string titleText: qsTr("作品 #%1").arg(postKey)
    signal selectionRequested(string key, bool desired, int revision)
    signal favoriteRequested(string key, bool desired, int revision)
    signal detailRequested(string key, int revision, int row)
    required property var cacheController
    property QtObject theme: AppTheme {}
    signal activated(int row)
    required property int index
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
    radius: 9
    color: theme.card
    border.color: theme.border

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

    function handleThumbnailStatus(
            token, url, cacheGeneration, status) {
        if (token !== thumbnailRebuildToken
                || url !== card.previewUrl)
            return
        if (status === Image.Ready) {
            thumbnailLoading = false
            const action = cacheController
                .decodeSuccessActionForGeneration(
                    url, cacheGeneration)
            thumbnailFailed = action !== "accepted"
        } else if (status === Image.Error) {
            thumbnailLoading = false
            const finalUrl = cacheController.finalUrlFor(url)
            const action = cacheController
                .decodeFailureActionForGeneration(
                    url, finalUrl, cacheGeneration)
            if (action === "retry")
                rebuildThumbnail()
            else
                thumbnailFailed = true
        }
    }

    function retryThumbnail() {
        cacheController.prepareManualRetry(
            card.previewUrl,
            cacheController.finalUrlFor(card.previewUrl)
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
        property var pressedAction: null
        onPressedChanged: {
            if (pressed)
                pressedAction = { "key": card.selectionKey, "revision": card.actionRevision, "row": card.index }
        }
        onTapped: function(point) {
            const p = point.position
            if (p.x >= cardActions.x && p.x <= cardActions.x + cardActions.width
                    && p.y >= cardActions.y && p.y <= cardActions.y + cardActions.height)
                return
            if (card.thumbnailFailed && p.y >= thumbnailLoader.y
                    && p.y <= thumbnailLoader.y + thumbnailLoader.height)
                return
            if (pressedAction) {
                card.activated(pressedAction.row)
                card.detailRequested(pressedAction.key, pressedAction.revision, pressedAction.row)
            }
        }
    }

    Row {
        id: cardActions
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: 12
        spacing: 4
        z: 2

        CheckBox {
            objectName: "postSelect_" + card.selectionKey
            checked: card.selected
            Accessible.name: qsTr("选择作品 #%1").arg(card.postKey)
            property var pressedAction: null
            onPressed: pressedAction = {
                "key": card.selectionKey,
                "revision": card.actionRevision,
                "selected": !checked
            }
            onClicked: {
                if (pressedAction)
                    card.selectionRequested(pressedAction.key,
                        pressedAction.selected, pressedAction.revision)
                pressedAction = null
            }
        }
        ActionButton {
            theme: card.theme
            objectName: "postFavorite_" + card.postKey
            width: 40
            leftPadding: 4
            rightPadding: 4
            text: card.favoriteKnown ? (card.favoriteSaved ? "★" : "☆") : "?"
            enabled: card.favoriteEnabled
            Accessible.name: card.favoriteSaved ? qsTr("取消本地收藏") : qsTr("本地收藏")
            ToolTip.visible: hovered
            ToolTip.text: !card.favoriteKnown ? qsTr("本地收藏状态未知，请重试")
                         : card.favoriteSaved ? qsTr("取消本地收藏") : qsTr("本地收藏")
            property var pressedAction: null
            onPressed: pressedAction = {
                "key": card.selectionKey,
                "revision": card.actionRevision,
                "desired": !card.favoriteSaved
            }
            onClicked: {
                if (pressedAction)
                    card.favoriteRequested(pressedAction.key,
                        pressedAction.desired, pressedAction.revision)
                pressedAction = null
            }
        }
    }

    Loader {
        id: thumbnailLoader
        objectName: "thumbnailLoader_" + card.postKey
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
            objectName: "thumbnail_" + card.postKey
            anchors.fill: parent
            source: ""
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            cache: false
            property int frozenToken: 0
            property int frozenCacheGeneration: 0
            property string frozenUrl: ""
            Component.onCompleted: {
                frozenToken = card.thumbnailRebuildToken
                frozenCacheGeneration = cacheController.cacheGeneration
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
        objectName: "thumbnailError_" + card.postKey
        anchors.fill: thumbnailLoader
        color: theme.raised
        visible: card.thumbnailFailed
        Column {
            anchors.centerIn: parent
            width: parent.width - 24
            spacing: theme.smallGap
            Label {
                width: parent.width
                text: qsTr("此缩略图加载失败")
                color: theme.error
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
            }
            Button {
                objectName: "thumbnailRetry_" + card.postKey
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
            radius: 5
            color: theme.mediaBadge
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
        radius: 5
        color: theme.ratingColor(card.ratingCode)
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
            text: card.titleText
            color: theme.text
            font.bold: true
            elide: Text.ElideRight
        }
        Label {
            Layout.fillWidth: true
            text: qsTr("得分 %1 · 收藏 %2")
                  .arg(card.scoreText).arg(card.favoriteText)
            color: theme.secondaryText
            elide: Text.ElideRight
        }
        Label {
            Layout.fillWidth: true
            text: qsTr("%1 · %2")
                  .arg(card.fileExtension).arg(card.dimensionsText)
            color: theme.mutedText
            elide: Text.ElideRight
        }
    }
}
