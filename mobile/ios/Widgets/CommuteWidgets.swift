import ActivityKit
import AlarmKit
import AppIntents
import SwiftUI
import WidgetKit

@main
struct CommuteWidgets: WidgetBundle {
  var body: some Widget {
    WakeCountdownWidget()
    TripProgressWidget()
  }
}

private struct WakeCountdownWidget: Widget {
  var body: some WidgetConfiguration {
    ActivityConfiguration(for: AlarmAttributes<WakeAlarmMetadata>.self) { context in
      HStack {
        VStack(alignment: .leading) {
          Label("Wake alarm", systemImage: "alarm")
          WakeCountdown(state: context.state)
            .font(.title2)
        }
        Spacer()
        stopButton(context.state.alarmID)
      }
      .padding()
    } dynamicIsland: { context in
      DynamicIsland {
        DynamicIslandExpandedRegion(.leading) {
          Label("Wake alarm", systemImage: "alarm")
        }
        DynamicIslandExpandedRegion(.trailing) {
          stopButton(context.state.alarmID)
        }
        DynamicIslandExpandedRegion(.bottom) {
          WakeCountdown(state: context.state)
            .font(.title2)
        }
      } compactLeading: {
        Image(systemName: "alarm")
      } compactTrailing: {
        WakeCountdown(state: context.state)
          .frame(maxWidth: 48)
      } minimal: {
        Image(systemName: "alarm")
      }
      .keylineTint(.indigo)
    }
  }

  private func stopButton(_ id: UUID) -> some View {
    Button(intent: StopWakeCountdown(id: id)) {
      Image(systemName: "stop.fill")
        .accessibilityLabel("Stop wake alarm")
    }
    .buttonStyle(.borderedProminent)
    .tint(.indigo)
  }
}

private struct WakeCountdown: View {
  let state: AlarmPresentationState

  var body: some View {
    Group {
      switch state.mode {
      case .countdown(let countdown):
        Text(timerInterval: min(countdown.startDate, countdown.fireDate)...countdown.fireDate)
      case .paused(let paused):
        let remaining = max(0, paused.totalCountdownDuration - paused.previouslyElapsedDuration)
        Text(Duration.seconds(remaining).formatted(.time(pattern: .minuteSecond)))
      case .alert:
        Text("Ringing")
      @unknown default:
        Text("Wake alarm")
      }
    }
    .monospacedDigit()
    .lineLimit(1)
    .minimumScaleFactor(0.65)
  }
}

private struct TripProgressWidget: Widget {
  var body: some WidgetConfiguration {
    ActivityConfiguration(for: TripActivityAttributes.self) { context in
      VStack(alignment: .leading, spacing: 8) {
        Label("Commute", systemImage: "train.side.front.car")
          .font(.headline)
        tripDetails(context)
      }
      .padding()
    } dynamicIsland: { context in
      DynamicIsland {
        DynamicIslandExpandedRegion(.leading) {
          Label("Commute", systemImage: "train.side.front.car")
        }
        DynamicIslandExpandedRegion(.bottom) {
          tripDetails(context)
        }
      } compactLeading: {
        Image(systemName: context.isStale ? "exclamationmark.circle" : "train.side.front.car")
      } compactTrailing: {
        Text(timerInterval: interval(context))
          .monospacedDigit()
          .frame(maxWidth: 48)
          .minimumScaleFactor(0.65)
      } minimal: {
        Image(systemName: context.isStale ? "exclamationmark.circle" : "train.side.front.car")
      }
      .keylineTint(.indigo)
    }
  }

  private func tripDetails(_ context: ActivityViewContext<TripActivityAttributes>) -> some View {
    VStack(alignment: .leading, spacing: 6) {
      HStack {
        Text(context.isStale ? "Last estimated arrival" : "Estimated arrival")
        Spacer()
        Text(context.state.estimatedArrival, style: .time)
          .monospacedDigit()
      }
      ProgressView(timerInterval: interval(context), countsDown: false) {
        Text("Estimated trip progress")
      } currentValueLabel: {
        Text(timerInterval: interval(context))
          .monospacedDigit()
      }
      if context.isStale {
        Text("Open the app to refresh the timetable.")
          .foregroundStyle(.secondary)
          .font(.footnote)
      }
    }
  }

  private func interval(_ context: ActivityViewContext<TripActivityAttributes>) -> ClosedRange<Date>
  {
    context.attributes.startedAt...max(context.attributes.startedAt, context.state.estimatedArrival)
  }
}
