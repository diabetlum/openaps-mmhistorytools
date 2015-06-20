from collections import defaultdict
from copy import copy
from datetime import datetime
from datetime import timedelta
from dateutil import parser

from .models import Bolus, Meal, TempBasal, Unit


class ParseHistory(object):
    DURATION_IN_MINUTES_KEY = "duration (min)"

    @staticmethod
    def _event_datetime(event):
        return parser.parse(event["timestamp"])


class CleanHistory(ParseHistory):
    """Analyze Medtronic pump history and resolves basic inconsistencies

    Responsibilities:
    - De-duplicates bolus wizard entries
    - Ensures suspend/resume records exist in pairs (inserting an extra event as necessary)
    - Removes any records not in the start_datetime to end_datetime window
    """
    def __init__(self, pump_history, start_datetime=None, end_datetime=None):
        """Initializes a new instance of the history parser

        :param pump_history: A list of pump history events, in reverse-chronological order
        :type pump_history: list(dict)
        :param start_datetime: The start time of history events. If not provided, the oldest
        record's timestamp is used
        :type start_datetime: datetime
        :param end_datetime: The end time of history events. If not provided, the latest record's
        timestamp is used
        :type end_datetime: datetime
        """
        self.clean_history = []
        self.start_datetime = start_datetime
        self.end_datetime = end_datetime

        if len(pump_history) > 0:
            if self.start_datetime is None:
                self.start_datetime = self._event_datetime(pump_history[-1])

            if self.end_datetime is None:
                self.end_datetime = self._event_datetime(pump_history[0])

        # Temporary parsing state
        self._boluswizard_events_by_body = defaultdict(list)
        self._last_resume_event = None
        self._last_temp_basal_duration_event = None

        for event in pump_history:
            self.add_history_event(event)

        # The pump was suspended before the history window began
        if self._last_resume_event is not None:
            self.add_history_event({
                "_type": "PumpSuspend",
                "timestamp": self.start_datetime.isoformat()
            })

    def _filter_events_in_range(self, events):
        start_datetime = self.start_datetime
        end_datetime = self.end_datetime

        def timestamp_in_range(event):
            if event:
                timestamp = self._event_datetime(event)
                if start_datetime <= timestamp <= end_datetime:
                    return True
            return False

        return filter(timestamp_in_range, events)

    def add_history_event(self, event):
        try:
            decoded = getattr(self, "_decode_{}".format(event["_type"].lower()))(event)
        except AttributeError:
            decoded = [event]

        self.clean_history.extend(self._filter_events_in_range(decoded or []))

    def _decode_boluswizard(self, event):
        event_datetime = self._event_datetime(event)

        # BolusWizard records can appear as duplicates with one containing appended data.
        # Criteria are records are less than 1 min apart and have identical bodies
        for seen_event in self._boluswizard_events_by_body[event["_body"]]:
            if abs(self._event_datetime(seen_event) - event_datetime) <= timedelta(minutes=1):
                return None

        self._boluswizard_events_by_body[event["_body"]].append(event)

        return [event]

    def _decode_pumpresume(self, event):
        self._last_resume_event = event

        return [event]

    def _decode_pumpsuspend(self, event):
        events = [event]

        if self._last_resume_event is None:
            events.insert(0, {
                "_type": "PumpResume",
                "timestamp": self.end_datetime.isoformat(),
            })
        else:
            self._last_resume_event = None

        return events

    def _decode_tempbasal(self, event):
        assert self._last_temp_basal_duration_event["timestamp"] == event["timestamp"], \
            "Partial temp basal record found. Please re-run with a larger history window."

        return [event]

    def _decode_tempbasalduration(self, event):
        self._last_temp_basal_duration_event = event

        return [event]


class ReconcileHistory(ParseHistory):
    """Analyze Medtronic pump history and reconciles dependencies between records

    Responsibilities:
    - Modifies temporary basal duration to account for cancelled and overlapping basals
    - Duplicates and modifies temporary basal records to account for delivery pauses when suspended
    """
    def __init__(self, clean_history):
        """Initializes a new instance of the history parser

        The input history is expected to have no open-ended suspend windows, which can be resolved
        by the CleanHistory class.

        :param clean_history: A list of pump history events in reverse-chronological order
        :type clean_history: list(dict)
        """
        self.reconciled_history = []

        # Temporary parsing state
        self._last_suspend_event = None
        self._last_temp_basal_event = None
        self._last_temp_basal_duration_event = None

        for event in reversed(clean_history):
            self.add_history_event(event)

    def add_history_event(self, event):
        try:
            decoded = getattr(self, "_decode_{}".format(event["_type"].lower()))(event)
        except AttributeError:
            decoded = [event]

        for decoded_event in decoded:
            self.reconciled_history.insert(0, decoded_event)

    def _basal_event_datetimes(self, basal_event):
        basal_start_datetime = self._event_datetime(basal_event)
        basal_end_datetime = basal_start_datetime + timedelta(
            minutes=basal_event[self.DURATION_IN_MINUTES_KEY]
        )
        return basal_start_datetime, basal_end_datetime

    def _trim_last_temp_basal_to_datetime(self, trim_datetime):
        if self._last_temp_basal_duration_event is not None:
            basal_event = self._last_temp_basal_duration_event
            basal_start_datetime, basal_end_datetime = self._basal_event_datetimes(basal_event)

            if basal_end_datetime > trim_datetime:
                basal_event[self.DURATION_IN_MINUTES_KEY] = int(
                    (trim_datetime - basal_start_datetime).total_seconds() / 60.0
                )

    def _decode_pumpresume(self, event):
        events = [event]

        if self._last_temp_basal_duration_event is not None:
            suspend_datetime = self._event_datetime(self._last_suspend_event)
            resume_datetime = self._event_datetime(event)
            basal_duration_event = self._last_temp_basal_duration_event
            _, basal_end_datetime = self._basal_event_datetimes(basal_duration_event)

            self._trim_last_temp_basal_to_datetime(suspend_datetime)

            if basal_end_datetime > resume_datetime:
                # Duplicate and restart the temp basal still scheduled
                new_basal_duration_event = copy(basal_duration_event)
                new_basal_rate_event = copy(self._last_temp_basal_event)

                # Adjust start time
                for new_event in (new_basal_duration_event, new_basal_rate_event):
                    for key in ("date", "_date", "timestamp"):
                        new_event[key] = event[key]
                    new_event["_description"] = "{} generated due to interleaved PumpSuspend" \
                                                " event".format(new_event["_type"])

                # Adjust duration
                new_basal_duration_event[self.DURATION_IN_MINUTES_KEY] = int(
                    (basal_end_datetime - resume_datetime).total_seconds() / 60.0
                )

                events.append(new_basal_rate_event)
                events.append(new_basal_duration_event)

        return events

    def _decode_pumpsuspend(self, event):
        self._last_suspend_event = event

        return [event]

    def _decode_tempbasal(self, event):
        self._last_temp_basal_event = event

        return [event]

    def _decode_tempbasalduration(self, event):
        self._trim_last_temp_basal_to_datetime(self._event_datetime(event))

        self._last_temp_basal_duration_event = event

        return [event]


class ResolveHistory(ParseHistory):
    """Converts Medtronic pump history to a sequence of general record types

    Each record is a dictionary representing one of the following types:

    - `Bolus`: Fast insulin delivery events in Units
    - `Meal`: Grams of carbohydrate
    - `TempBasal`: Paced insulin delivery events in Units/hour, or Percent of scheduled basal

    The following history events are parsed:

    - TempBasal and TempBasalDuration are combined into TempBasal records
    - PumpSuspend and PumpResume are combined into TempBasal records of 0%
    - Square Bolus is converted to a TempBasal record
    - Normal Bolus is converted to a Bolus record
    - BolusWizard carb entry is converted to a Meal record
    - JournalEntryMealMarker is converted to a Meal record

    Events that are not related to the record types or seem to have no effect are dropped.
    """
    def __init__(self, reconciled_history, current_datetime=None):
        """Initializes a new instance of the history parser

        The input history is expected to have no open-ended suspend windows, which can be resolved
        by the CleanHistory class.

        If not provided, `current_datetime` will default to datetime.now(), which is assumed to be
        a naive datetime in the same timezone as the pump. This is not a safe assumption to make
        for most fresh Raspberry Pi setups.

        :param reconciled_history: A list of pump history events in reverse-chronological order
        :type reconciled_history: list(dict)
        :param current_datetime: The datetime at which the history was generated
        :type current_datetime: datetime
        """
        self.resolved_history = []
        self.current_datetime = current_datetime or datetime.now()

        # Temporary parsing state
        self._resume_datetime = None
        self._temp_basal_duration = None

        for event in reconciled_history:
            self.add_history_event(event)

    def add_history_event(self, event):
        try:
            decoded = getattr(self, "_decode_{}".format(event["_type"].lower()))(event)
        except AttributeError:
            pass
        else:
            if decoded is not None:
                self.resolved_history.append(decoded)

    def _decode_bolus(self, event):
        start_at = self._event_datetime(event)
        delivered = event["amount"]
        programmed = event["programmed"]

        if max(delivered, programmed) > 0:
            if event["type"] == "square":
                duration = event["duration"]

                rate = programmed / (duration / 60.0)

                # If less than 100% of the programmed dose was delivered and we're past the delivery
                # window, then shorten the actual duration by the ratio of delivered insulin.
                if start_at + timedelta(minutes=duration) < self.current_datetime:
                    duration = int(duration * delivered / programmed)

                return TempBasal(
                    start_at=start_at,
                    end_at=start_at + timedelta(minutes=duration),
                    amount=rate,
                    unit=Unit.units_per_hour,
                    description="Square bolus: {}U over {}min".format(delivered, duration)
                )

            else:
                return Bolus(
                    start_at=start_at,
                    end_at=start_at,
                    amount=delivered,
                    unit=Unit.units,
                    description="Normal bolus: {}U".format(delivered)
                )

    def _decode_boluswizard(self, event):
        return self._decode_journalentrymealmarker(event)

    def _decode_journalentrymealmarker(self, event):
        carb_input = event["carb_input"]
        start_at = self._event_datetime(event)

        if carb_input:
            return Meal(
                start_at=start_at,
                end_at=start_at,
                amount=carb_input,
                unit=Unit.grams,
                description=event["_type"]
            )

    def _decode_pumpresume(self, event):
        self._resume_datetime = self._event_datetime(event)

    def _decode_pumpsuspend(self, event):
        assert self._resume_datetime is not None, "Unbalanced Suspend/Resume events found"

        start_at = self._event_datetime(event)
        end_at = self._resume_datetime

        self._resume_datetime = None

        if end_at > start_at:
            return TempBasal(
                start_at=self._event_datetime(event),
                end_at=end_at,
                amount=0,
                unit=Unit.percent_of_basal,
                description="Pump Suspend"
            )

    def _decode_tempbasal(self, event):
        assert self._temp_basal_duration is not None, "Temp basal duration not found"

        start_at = self._event_datetime(event)
        end_at = start_at + timedelta(minutes=self._temp_basal_duration)

        if end_at > start_at:
            amount = event["rate"]
            unit = Unit.percent_of_basal if event["temp"] == "percent" else Unit.units_per_hour

            return TempBasal(
                start_at=start_at,
                end_at=end_at,
                amount=amount,
                unit=unit,
                description="TempBasal {} {}".format(amount, unit)
            )

    def _decode_tempbasalduration(self, event):
        self._temp_basal_duration = event[self.DURATION_IN_MINUTES_KEY]
