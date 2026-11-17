import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: panel
    objectName: "poolMembershipPanel"
    required property var controller
    required property var navigation
    property QtObject theme: AppTheme {}
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(640, parent ? parent.width - 32 : 640)
    height: Math.min(600, parent ? parent.height - 40 : 600)
    modal: true
    title: qsTr("所属作品池")
    onClosed: navigation.cancelMemberships()
    contentItem: ColumnLayout {
        spacing: 12
        Label {
            Layout.fillWidth: true
            text: qsTr("系列作品池保留连续页序；主题作品池以集合展示。")
            color: theme.secondaryText; wrapMode: Text.Wrap
        }
        Label { text: qsTr("正在读取……"); visible: controller.membershipBusy; color: theme.secondaryText }
        Label {
            objectName: "poolMembershipError"
            Layout.fillWidth: true; visible: text.length > 0
            text: controller.membershipError; color: theme.error; wrapMode: Text.Wrap
        }
        ListView {
            objectName: "poolMembershipList"
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            spacing: 8; model: controller.memberships
            ScrollBar.vertical: ScrollBar {}
            delegate: ActionButton {
                required property var modelData
                objectName: "poolMembership_" + modelData.poolKey
                width: ListView.view.width
                text: modelData.name + " · " + modelData.categoryText + " · " + modelData.postCount
                property var pressedAction: null
                onPressed: pressedAction = {"key": modelData.poolKey, "revision": controller.membershipRevision}
                onClicked: {
                    if (pressedAction && navigation.openMembership(pressedAction.key, pressedAction.revision))
                        panel.close()
                    pressedAction = null
                }
            }
            Label {
                anchors.centerIn: parent
                visible: !controller.membershipBusy && !controller.membershipError.length && controller.memberships.length === 0
                text: qsTr("本页没有返回所属作品池。")
                color: theme.secondaryText
            }
        }
        Flow {
            Layout.fillWidth: true; spacing: 8
            ActionButton {
                objectName: "previousPoolMembershipsButton"; text: qsTr("上一页")
                enabled: controller.membershipPage > 1 && !controller.membershipBusy
                onClicked: controller.showMembershipPage(controller.membershipPage - 1, controller.membershipRevision)
            }
            Label { text: qsTr("第 %1 页 · 每页最多20项").arg(controller.membershipPage); height: 40; verticalAlignment: Text.AlignVCenter; color: theme.secondaryText }
            ActionButton {
                objectName: "nextPoolMembershipsButton"; text: qsTr("下一页")
                enabled: controller.memberships.length === 20 && controller.membershipPage < 1000 && !controller.membershipBusy
                onClicked: controller.showMembershipPage(controller.membershipPage + 1, controller.membershipRevision)
            }
            ActionButton { objectName: "closePoolMembershipsButton"; text: qsTr("关闭"); onClicked: panel.close() }
        }
    }
}
