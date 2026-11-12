import QtQuick
import QtQuick.Controls

GridView {
    id: grid
    objectName: "galleryGrid"
    required property var favoriteSelection
    required property var cacheController
    property QtObject theme: AppTheme {}
    signal postActivated(int row)
    clip: true
    cellWidth: Math.max(225, width / Math.max(1, Math.floor(width / 245)))
    cellHeight: 340
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    delegate: PostCard {
        favoriteSelection: grid.favoriteSelection
        cacheController: grid.cacheController
        theme: grid.theme
        onActivated: function(row) { grid.postActivated(row) }
    }
}
