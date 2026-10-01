import Foundation
import UserNotifications
import SaintCore

/// Reminders and timers as local notifications, so they ring when SAINT isn't running. The reminder store only
/// describes them (NotificationPlan); this turns each into something iOS delivers on its own.
final class ReminderCenter: NSObject, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private let prefix = "saint."
    /// iOS keeps at most 64 pending notifications per app.
    private let limit = 60
    /// Called when a notification arrives while SAINT is open, so it can be spoken too.
    var onForegroundNotification: ((String, String) -> Void)?
    /// How long "Snooze" on a reminder waits (Settings).
    var snoozeMinutes: (() -> Int)?
    private let categoryID = "saint.reminder"
    private let snoozeAction = "saint.snooze"
    private let snoozePrefix = "saintsnooze."          // not "saint.": reschedule() mustn't remove snoozed ones

    override init() {
        super.init()
        center.delegate = self
        // Like the Clock and Reminders apps: Snooze and Dismiss right on the notification.
        let snooze = UNNotificationAction(identifier: snoozeAction, title: "Snooze", options: [])
        let dismiss = UNNotificationAction(identifier: "saint.dismiss", title: "Dismiss", options: [.destructive])
        center.setNotificationCategories([UNNotificationCategory(identifier: categoryID, actions: [snooze, dismiss],
                                                                 intentIdentifiers: [], options: [.customDismissAction])])
    }

    func requestAuthorization() async -> Bool {
        (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
    }

    func isAuthorized() async -> Bool {
        let settings = await center.notificationSettings()
        return settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
    }

    /// Replace everything SAINT scheduled with the current plans.
    func reschedule(_ plans: [NotificationPlan]) async {
        let pending = await center.pendingNotificationRequests()
        let ours = pending.map { $0.identifier }.filter { $0.hasPrefix(prefix) }
        center.removePendingNotificationRequests(withIdentifiers: ours)
        var added = 0
        for plan in plans {
            for request in requests(for: plan) {
                if added >= limit { return }
                try? await center.add(request)
                added += 1
            }
        }
    }

    private func makeContent(_ plan: NotificationPlan) -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = plan.title
        content.body = plan.body
        content.sound = .default
        content.threadIdentifier = "saint.reminders"
        content.categoryIdentifier = categoryID
        return content
    }

    private func requests(for plan: NotificationPlan) -> [UNNotificationRequest] {
        let content = makeContent(plan)
        switch plan.trigger {
        case .at(let date):
            let parts = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
            let trigger = UNCalendarNotificationTrigger(dateMatching: parts, repeats: false)
            return [UNNotificationRequest(identifier: "\(prefix)\(plan.id)", content: content, trigger: trigger)]
        case .every(let seconds):
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: TimeInterval(max(60, seconds)), repeats: true)
            return [UNNotificationRequest(identifier: "\(prefix)\(plan.id)", content: content, trigger: trigger)]
        case .daily(let hour, let minute, let weekdays):
            if weekdays.isEmpty {
                var parts = DateComponents()
                parts.hour = hour
                parts.minute = minute
                let trigger = UNCalendarNotificationTrigger(dateMatching: parts, repeats: true)
                return [UNNotificationRequest(identifier: "\(prefix)\(plan.id)", content: content, trigger: trigger)]
            }
            return weekdays.map { day in
                var parts = DateComponents()
                parts.hour = hour
                parts.minute = minute
                parts.weekday = day
                let trigger = UNCalendarNotificationTrigger(dateMatching: parts, repeats: true)
                return UNNotificationRequest(identifier: "\(prefix)\(plan.id).\(day)", content: content, trigger: trigger)
            }
        }
    }

    // MARK: delegate

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        onForegroundNotification?(notification.request.content.title, notification.request.content.body)
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if response.actionIdentifier == snoozeAction {
            let original = response.notification.request.content
            let content = UNMutableNotificationContent()
            content.title = original.title
            content.body = original.body
            content.sound = .default
            content.threadIdentifier = original.threadIdentifier
            content.categoryIdentifier = categoryID
            let minutes = max(1, snoozeMinutes?() ?? 10)
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: TimeInterval(minutes * 60), repeats: false)
            center.add(UNNotificationRequest(identifier: snoozePrefix + UUID().uuidString, content: content, trigger: trigger))
        }
        completionHandler()
    }
}
