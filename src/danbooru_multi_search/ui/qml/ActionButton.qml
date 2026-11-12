import QtQuick
import QtQuick.Controls

Button {
    id: control
    property QtObject theme: AppTheme {}
    property bool primary: false
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
                                : control.primary ? control.theme.accentForeground
                                : control.checked ? control.theme.accentText
                                : control.theme.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: control.theme.smallRadius
        visible: !control.flat || control.hovered || control.down || control.checked
        color: !control.enabled ? control.theme.raised
               : control.primary
                 ? (control.down ? Qt.darker(control.theme.accent, 1.15)
                    : control.hovered ? Qt.lighter(control.theme.accent, 1.08)
                    : control.theme.accent)
                 : control.checked ? control.theme.selected
                 : control.down || control.hovered
                   ? control.theme.raised : control.theme.surface
        border.width: control.visualFocus ? 2 : 1
        border.color: control.visualFocus
                      ? (control.primary ? control.theme.text : control.theme.accent)
                      : (control.primary || control.checked) && control.enabled
                        ? control.theme.accent : control.theme.border
    }
}
