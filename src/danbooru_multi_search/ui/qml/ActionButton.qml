import QtQuick
import QtQuick.Controls

Button {
    id: control
    property QtObject theme: AppTheme {}
    property bool primary: false
    readonly property color fillColor: !enabled ? theme.raised
        : primary ? (down ? Qt.darker(theme.accent, 1.15)
                     : hovered ? Qt.lighter(theme.accent, 1.08) : theme.accent)
        : checked ? theme.selected
        : down || hovered ? theme.raised : theme.surface
    readonly property color backgroundColor: Qt.rgba(fillColor.r, fillColor.g, fillColor.b,
        fillColor.a * (1 - theme.buttonBackgroundTransparency))
    readonly property color primaryTextColor: {
        if (theme.buttonBackgroundTransparency === 0)
            return theme.accentForeground
        const surface = over(theme.surface, theme.background)
        const fill = over(backgroundColor, surface)
        return contrast(theme.text, fill) > contrast(theme.accentForeground, fill)
                ? theme.text : theme.accentForeground
    }

    // Compare the visible fill with the two existing foreground choices. The
    // text itself stays opaque even when the fill becomes fully transparent.
    function over(foreground, background) {
        const alpha = foreground.a
        return Qt.rgba(foreground.r * alpha + background.r * (1 - alpha),
                       foreground.g * alpha + background.g * (1 - alpha),
                       foreground.b * alpha + background.b * (1 - alpha), 1)
    }
    function luminance(color) {
        function channel(value) {
            return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b)
    }
    function contrast(first, second) {
        const a = luminance(first), b = luminance(second)
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
    }
    highlighted: primary
    implicitWidth: Math.max(80, implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: theme.controlHeight
    leftPadding: 16
    rightPadding: 16
    hoverEnabled: true

    contentItem: Label {
        text: control.text
        font: control.font
        color: !control.enabled ? control.theme.subtleText
                                : control.primary ? control.primaryTextColor
                                : control.checked ? control.theme.accentText
                                : control.theme.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: control.theme.smallRadius
        visible: !control.flat || control.hovered || control.down || control.checked
        color: control.backgroundColor
        border.width: control.visualFocus
                      || (control.theme.buttonBackgroundTransparency > 0 && control.down) ? 2 : 1
        border.color: control.visualFocus
                      ? (control.primary ? control.theme.text : control.theme.accent)
                      : control.theme.buttonBackgroundTransparency > 0 && control.enabled
                        ? (control.down || control.checked ? control.theme.accent
                           : control.hovered ? control.theme.borderStrong
                           : control.primary ? control.theme.accent : control.theme.border)
                        : (control.primary || control.checked) && control.enabled
                          ? control.theme.accent : control.theme.border
    }
}
