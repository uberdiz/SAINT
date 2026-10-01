import SwiftUI
import SaintCore

struct RemindersView: View {
    @EnvironmentObject var model: AppModel
    @State private var adding = false

    private struct Upcoming: Identifiable {
        let reminder: Reminder
        let due: Date
        var id: String { reminder.id }
    }

    private var upcoming: [Upcoming] {
        _ = model.dataVersion
        return model.brain.reminders.upcoming().map { Upcoming(reminder: $0.reminder, due: $0.due) }
    }

    private var finished: [Reminder] {
        _ = model.dataVersion
        return model.brain.reminders.all().filter { !$0.isActive }.prefix(20).map { $0 }
    }

    var body: some View {
        NavigationStack {
            List {
                if upcoming.isEmpty {
                    Section {
                        VStack(spacing: 8) {
                            Image(systemName: "bell.badge").font(.largeTitle).foregroundStyle(.secondary)
                            Text("Nothing coming up").font(.headline)
                            Text("Say “SAINT, remind me at 5 to call mom”, or tap +.")
                                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 24)
                    }
                    .listRowBackground(Color.clear)
                } else {
                    Section("Upcoming") {
                        ForEach(upcoming) { item in
                            ReminderRow(reminder: item.reminder, due: item.due)
                                .swipeActions {
                                    Button(role: .destructive) { model.brain.reminders.cancel(id: item.reminder.id) } label: {
                                        Label("Cancel", systemImage: "xmark.bin")
                                    }
                                }
                        }
                    }
                }
                if !finished.isEmpty {
                    Section("Earlier") {
                        ForEach(finished) { r in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(r.message).strikethrough(r.status == ReminderStatus.cancelled)
                                Text(r.status.capitalized).font(.caption).foregroundStyle(.secondary)
                            }
                            .swipeActions {
                                Button(role: .destructive) { model.brain.reminders.remove(id: r.id) } label: {
                                    Label("Delete", systemImage: "trash")
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Reminders")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { adding = true } label: { Image(systemName: "plus") }.accessibilityLabel("Add reminder")
                }
            }
            .sheet(isPresented: $adding) { AddReminderSheet() }
            .overlay(alignment: .bottom) { timerBar }
        }
    }

    private var timerBar: some View {
        HStack(spacing: 8) {
            ForEach([5, 10, 25], id: \.self) { minutes in
                Button("\(minutes) min") {
                    let at = Date().addingTimeInterval(Double(minutes) * 60)
                    model.brain.reminders.add(message: "Timer finished", schedule: .once(at: at), isTimer: true)
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(10)
        .glassCard(radius: 24)
        .padding(.bottom, 8)
    }
}

struct ReminderRow: View {
    let reminder: Reminder
    let due: Date

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: reminder.isTimer ? "timer" : (reminder.schedule.isRecurring ? "repeat" : "bell"))
                .foregroundStyle(Theme.accent)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(reminder.message).font(.system(.body, design: .rounded, weight: .medium))
                Text(TimeParse.describe(reminder.schedule)).font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            Text(due, style: .relative).font(.caption).foregroundStyle(.secondary)
        }
    }
}

struct AddReminderSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var message = ""
    @State private var when = Date().addingTimeInterval(3600)
    @State private var repeatMode = 0           // 0 once, 1 every day, 2 weekdays, 3 every week
    @State private var spoken = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Say it") {
                    TextField("“remind me at 5 to call mom”", text: $spoken)
                        .submitLabel(.done)
                        .onSubmit(addSpoken)
                    Text("Works in any language SAINT knows.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Or set it") {
                    TextField("What", text: $message)
                    DatePicker("When", selection: $when, in: Date()...)
                    Picker("Repeat", selection: $repeatMode) {
                        Text("Once").tag(0)
                        Text("Every day").tag(1)
                        Text("Weekdays").tag(2)
                        Text("Every week").tag(3)
                    }
                }
            }
            .navigationTitle("New reminder")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") { addForm() }.disabled(message.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }

    private func addSpoken() {
        let text = spoken
        Task {
            await model.submit(text)
            dismiss()
        }
    }

    private func addForm() {
        let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
        let cal = Calendar.current
        let parts = cal.dateComponents([.hour, .minute], from: when)
        let clock = String(format: "%02d:%02d", parts.hour ?? 9, parts.minute ?? 0)
        let schedule: Schedule
        switch repeatMode {
        case 1: schedule = .daily(time: clock, days: nil)
        case 2: schedule = .daily(time: clock, days: [0, 1, 2, 3, 4])
        case 3:
            let weekday = (cal.component(.weekday, from: when) + 5) % 7          // Apple: 1 = Sunday -> ours: 0 = Monday
            schedule = .daily(time: clock, days: [weekday])
        default: schedule = .once(at: when)
        }
        model.brain.reminders.add(message: text, schedule: schedule)
        dismiss()
    }
}
