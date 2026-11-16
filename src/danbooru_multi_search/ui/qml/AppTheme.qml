import QtQuick

QtObject {
    readonly property bool dark: typeof appearanceController === "undefined"
                                 || appearanceController.effectiveDark
    readonly property real buttonBackgroundTransparency: typeof appearanceController === "undefined"
                                                        ? 0 : appearanceController.buttonBackgroundTransparency
    readonly property color background: dark ? "#121419" : "#eef1f6"
    readonly property color panel: dark ? "#ec181b22" : "#efffffff"
    readonly property color surface: dark ? "#e820242c" : "#f5ffffff"
    readonly property color card: dark ? "#ed191c23" : "#f5ffffff"
    readonly property color canvas: dark ? "#d90f1115" : "#dde4e9f1"
    readonly property color viewer: dark ? "#0b0d11" : "#d9dfe8"
    readonly property color input: dark ? "#111722" : "#f3f6fb"
    readonly property color raised: dark ? "#292e38" : "#e7ecf3"
    readonly property color border: dark ? "#343944" : "#cbd3df"
    readonly property color borderStrong: dark ? "#4b5362" : "#a5b3c7"
    readonly property color text: dark ? "#f4f4f5" : "#172235"
    readonly property color secondaryText: dark ? "#d4d4d8" : "#3f4f65"
    readonly property color mutedText: dark ? "#a1a1aa" : "#626d7d"
    readonly property color subtleText: dark ? "#71717a" : "#687487"
    readonly property color accent: dark ? "#60a5fa" : "#2266bc"
    readonly property color accentForeground: dark ? "#10243b" : "white"
    readonly property color accentText: dark ? "#93c5fd" : "#225ca5"
    readonly property color selected: dark ? "#2d4261" : "#dce9fb"
    readonly property color progressTrack: dark ? "#263244" : "#d9e5f5"
    readonly property color error: dark ? "#fda4af" : "#b42339"
    readonly property color warning: dark ? "#fbbf24" : "#875800"
    readonly property color success: dark ? "#86efac" : "#187243"
    readonly property color purple: dark ? "#c4b5fd" : "#7052aa"
    readonly property color mediaBadge: "#a23f52"
    readonly property int gap: 10
    readonly property int smallGap: 8
    readonly property int largeGap: 16
    readonly property int radius: 10
    readonly property int smallRadius: 6
    readonly property int fontSize: 14
    readonly property int smallFontSize: 12
    readonly property int controlHeight: 40

    function ratingColor(code) {
        if (code === "g") return "#3f8f5a"
        if (code === "s") return "#3976b8"
        if (code === "q") return "#9a601e"
        if (code === "e") return "#b64056"
        return "#626872"
    }
}
