#!/usr/bin/env python3
"""Own every Nav2 goal used for patrol and dashboard point navigation.

The node accepts ``start``, ``stop``, ``pause``, ``resume`` and ``goto`` JSON
commands on ``/patrol_control``. It is intentionally the only
``NavigateToPose`` action client in the dashboard/patrol path so a manual goal
cannot race a patrol goal.

Every control command advances ``generation``. Action responses, feedback,
results and one-shot timers capture that generation (and, for patrol goals,
the waypoint index). A callback from an older command therefore cannot change
the current mode or schedule another goal. If such a callback contains a newly
accepted goal handle, that handle is canceled immediately.
"""

import json
import math
import time

import rclpy
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String


# x, y, yaw triples. A real deployment normally sets wait_for_route:=true and
# receives a map-derived route from the dashboard.
DEFAULT_WAYPOINTS = [
    0.5, 3.5, 0.00,
    3.5, 1.0, 0.00,
    3.5, 6.0, 1.57,
    6.5, 6.0, -1.57,
    6.5, 1.0, 0.00,
    9.5, 3.5, 1.57,
    0.5, 3.5, 3.14,
]

MODES = {"idle", "patrolling", "paused", "navigating"}
PHASES = {
    "received", "canceling", "accepted", "rejected",
    "succeeded", "failed", "canceled",
}

# These are the two detections for which the patrol camera must remain at the
# scene briefly.  Keep the spelling identical to ai_inference_sender.py; the
# labels travel unchanged through vision_inference_node.py on /safety_status.
CRITICAL_HOLD_LABELS = frozenset({"fire", "person"})


def critical_labels_from_status(raw):
    """Return critical labels from one /safety_status JSON message."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()

    detections = payload.get("detections", [])
    if not isinstance(detections, list):
        return frozenset()
    labels = {
        str(item.get("object", "")).strip().lower()
        for item in detections
        if isinstance(item, dict)
    }
    return frozenset(labels & CRITICAL_HOLD_LABELS)


class PatrolNode(Node):
    def __init__(self):
        super().__init__('patrol_node')

        self.declare_parameter('waypoints', DEFAULT_WAYPOINTS)
        self.declare_parameter('dwell_seconds', 3.0)
        self.declare_parameter('retry_per_waypoint', 2)
        self.declare_parameter('retry_delay', 2.0)
        self.declare_parameter('cancel_timeout_seconds', 5.0)
        self.declare_parameter('loop', True)
        self.declare_parameter('wait_for_route', False)
        self.declare_parameter('critical_hold_seconds', 3.0)
        self.declare_parameter('critical_rearm_clear_seconds', 3.0)

        flat = list(self.get_parameter('waypoints').value)
        if self.get_parameter('wait_for_route').value:
            flat = []
        self.dwell = float(self.get_parameter('dwell_seconds').value)
        self.max_retry = int(self.get_parameter('retry_per_waypoint').value)
        self.retry_delay = float(self.get_parameter('retry_delay').value)
        self.cancel_timeout = float(
            self.get_parameter('cancel_timeout_seconds').value
        )
        self.loop = bool(self.get_parameter('loop').value)
        self.critical_hold_seconds = max(
            0.0, float(self.get_parameter('critical_hold_seconds').value)
        )
        self.critical_rearm_clear_seconds = max(
            0.0,
            float(self.get_parameter('critical_rearm_clear_seconds').value),
        )

        if flat and (len(flat) < 3 or len(flat) % 3 != 0):
            self.get_logger().error(
                f"waypoints must contain x, y, yaw triples (received {len(flat)} values)."
            )
            raise SystemExit

        self.waypoints = [
            (float(flat[i]), float(flat[i + 1]), float(flat[i + 2]))
            for i in range(0, len(flat), 3)
        ]
        self.route_source = "parameter" if self.waypoints else "none"

        self.index = 0
        self.retries = 0
        self.consecutive_failures = 0
        self.timer = None
        self.cancel_timer = None
        self.shutting_down = False
        self._pending_transition = None

        # Explicit operating state. ``paused`` remains as a compatibility
        # attribute for existing code that inspects the node directly.
        self.mode = "idle"
        self.paused = False
        self.generation = 0

        # Action state is recorded as soon as send_goal_async is called. This
        # also represents a goal whose response has not arrived yet.
        self.current_goal = None
        self.goal_serial = 0
        # (generation, waypoint index, goal_type, per-send serial).  generation
        # protects commands, index protects waypoint advances, and serial also
        # separates retries of the same waypoint.
        self._active_goal_key = None
        self.active_goal = None            # {x, y, yaw}
        self.active_goal_type = None        # patrol | goto
        self.active_request_id = None
        self.active_command = None
        self.distance_remaining = None

        # A paused manual goal can be resumed. A paused patrol only needs its
        # current index, which is deliberately left unchanged.
        self._paused_goal_type = None
        self._paused_goal = None

        # A critical object is latched until it has been absent continuously.
        # ai_inference_sender publishes every inference frame, so without this
        # latch the same fire/person would start another three-second pause as
        # soon as the automatic resume sent the next Nav2 goal.
        self._critical_hold_latched = False
        self._critical_last_seen = None
        self._critical_hold_request_id = None

        self.navigation = {
            "request_id": None,
            "command": None,
            "goal_type": None,
            "phase": None,
            "status": None,
            "state": self.mode,
            "goal": None,
            "message": "No navigation command has been received.",
            "error": None,
            "distance_remaining": None,
            "generation": self.generation,
            "waypoint_index": None,
            "waypoint_total": len(self.waypoints),
        }

        self.create_subscription(String, '/patrol_control', self._on_control, 10)
        self.create_subscription(
            String, '/safety_status', self._on_safety_status, 10
        )

        route_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.route_pub = self.create_publisher(String, '/patrol_route', route_qos)
        self.status_pub = self.create_publisher(String, '/patrol_status', 10)
        self.navigation_status_pub = self.create_publisher(String, '/navigation_status', 10)
        self.create_timer(1.0, self._publish_status)

        self.nav_client = ActionClient(self, NavigateToPose, 'navigate_to_pose')
        self._publish_route()
        self._publish_status()

        if self.waypoints:
            self.get_logger().info(f"Loaded {len(self.waypoints)} patrol waypoints.")
        else:
            self.get_logger().info(
                "No patrol route yet; waiting for a start command with waypoints.")

        self.get_logger().info("Waiting for the 'navigate_to_pose' action server...")
        while not self.nav_client.wait_for_server(timeout_sec=1.0):
            if not rclpy.ok():
                raise SystemExit
            self.get_logger().info("Nav2 is not available yet; still waiting...")

        if self.waypoints:
            # Preserve the previous parameter-route auto-start behaviour while
            # giving it the same protected callback context as UI commands.
            self.generation += 1
            self.active_request_id = f"startup-{self.generation}"
            self.active_command = "start"
            self._set_mode("patrolling")
            self.send_goal(generation=self.generation, index=self.index)

        self.get_logger().info(
            "Critical detections %s pause patrol for %.1fs."
            % (sorted(CRITICAL_HOLD_LABELS), self.critical_hold_seconds)
        )

    # ------------------------------------------------------------------
    # State/status helpers
    # ------------------------------------------------------------------
    def _set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(f"invalid navigation mode: {mode}")
        self.mode = mode
        self.paused = mode == "paused"

    @staticmethod
    def _goal_dict(x, y, yaw):
        return {"x": float(x), "y": float(y), "yaw": float(yaw)}

    def _navigation_summary(self):
        summary = dict(self.navigation)
        if isinstance(summary.get("goal"), dict):
            summary["goal"] = dict(summary["goal"])
        return summary  

    def _publish_navigation(
        self,
        *,
        request_id,
        command,
        goal_type,
        phase,
        goal,
        message,
        generation,
        index=None,
        distance_remaining=None,
        refresh_embedded=True,
    ):
        if phase not in PHASES:
            raise ValueError(f"invalid navigation phase: {phase}")

        if distance_remaining is not None:
            try:
                distance_remaining = float(distance_remaining)
            except (TypeError, ValueError):
                distance_remaining = None
            if distance_remaining is not None and not math.isfinite(distance_remaining):
                distance_remaining = None

        self.distance_remaining = distance_remaining
        error = message if phase in {"rejected", "failed"} else None
        self.navigation = {
            "request_id": request_id,
            "command": command,
            "goal_type": goal_type,
            "phase": phase,
            # Aliases make the status straightforward for older/generic HTTP
            # bridges while ``phase`` and ``state`` keep distinct meanings.
            "status": phase,
            "state": self.mode,
            "goal": dict(goal) if isinstance(goal, dict) else None,
            "message": str(message),
            "error": error,
            "distance_remaining": distance_remaining,
            "generation": int(generation),
            "waypoint_index": index + 1 if index is not None else None,
            "waypoint_total": len(self.waypoints),
        }
        msg = String()
        msg.data = json.dumps(self.navigation)
        self.navigation_status_pub.publish(msg)

        # /patrol_status and /patrol_route retain all of their old top-level
        # fields and carry an additive navigation summary.
        if refresh_embedded:
            self._publish_status()
            self._publish_route()

    def _publish_route(self):
        msg = String()
        msg.data = json.dumps({
            "waypoints": [
                {"x": x, "y": y, "yaw": yaw} for x, y, yaw in self.waypoints
            ],
            "loop": bool(self.loop),
            "navigation": self._navigation_summary(),
        })
        self.route_pub.publish(msg)

    def _publish_status(self):
        msg = String()
        msg.data = json.dumps({
            "state": self.mode,
            "index": self.index + 1 if self.waypoints else 0,
            "total": len(self.waypoints),
            "source": self.route_source,
            "navigation": self._navigation_summary(),
        })
        self.status_pub.publish(msg)

    # ------------------------------------------------------------------
    # Timer/action cancellation and command generations
    # ------------------------------------------------------------------
    def _cancel_timer(self):
        timer, self.timer = self.timer, None
        if timer is None:
            return False
        try:
            timer.cancel()
        except Exception:  # noqa: BLE001 - best-effort cleanup during shutdown
            pass
        try:
            self.destroy_timer(timer)
        except Exception:  # noqa: BLE001 - timer may already be destroyed
            pass
        return True

    def schedule(self, seconds, callback, *, generation=None, index=None):
        """Schedule one callback, protected by its command generation/index."""
        self._cancel_timer()
        expected_generation = self.generation if generation is None else generation
        expected_index = self.index if index is None else index
        timer_holder = {}

        def once():
            timer = timer_holder.get("timer")
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:  # noqa: BLE001
                    pass
                if self.timer is timer:
                    self.timer = None
                try:
                    self.destroy_timer(timer)
                except Exception:  # noqa: BLE001
                    pass

            if self.shutting_down:
                return
            if expected_generation != self.generation:
                return
            if expected_index != self.index:
                return
            callback()

        timer = self.create_timer(float(seconds), once)
        timer_holder["timer"] = timer
        self.timer = timer

    def _cancel_current_goal(self):
        """Best-effort cancellation used only for shutdown/legacy callers.

        Runtime control commands use the confirmed transition path below.
        """
        handle, self.current_goal = self.current_goal, None
        had_active_goal = handle is not None or self._active_goal_key is not None
        self._active_goal_key = None
        self.active_goal = None
        self.active_goal_type = None
        self.distance_remaining = None

        if handle is not None:
            try:
                handle.cancel_goal_async()
            except Exception as exc:  # noqa: BLE001
                self.get_logger().warning(f"Could not cancel Nav2 goal: {exc}")
        return had_active_goal

    @staticmethod
    def _request_id(command, payload, generation, suffix=""):
        request_id = payload.get("request_id")
        if request_id is not None and str(request_id).strip():
            return str(request_id)
        tail = f"-{suffix}" if suffix else ""
        return f"{command}-{generation}{tail}"

    def _cancel_cancel_timer(self):
        timer, self.cancel_timer = self.cancel_timer, None
        if timer is None:
            return
        try:
            timer.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.destroy_timer(timer)
        except Exception:  # noqa: BLE001
            pass

    def _arm_cancel_timeout(self, transition):
        self._cancel_cancel_timer()
        holder = {}

        def timeout():
            timer = holder.get("timer")
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:  # noqa: BLE001
                    pass
                if self.cancel_timer is timer:
                    self.cancel_timer = None
                try:
                    self.destroy_timer(timer)
                except Exception:  # noqa: BLE001
                    pass
            if self._pending_transition is transition:
                self._fail_transition(
                    transition,
                    "Timed out while waiting for Nav2 cancellation confirmation.",
                )

        timer = self.create_timer(max(0.1, self.cancel_timeout), timeout)
        holder["timer"] = timer
        self.cancel_timer = timer

    def _reject_command(self, command, payload, goal_type, goal, message):
        request_id = self._request_id(
            command, payload, self.generation, suffix="rejected"
        )
        self._publish_navigation(
            request_id=request_id,
            command=command,
            goal_type=goal_type,
            phase="rejected",
            goal=goal,
            message=message,
            generation=self.generation,
        )

    def _begin_transition(
        self,
        command,
        payload,
        *,
        goal_type,
        status_goal,
        continuation,
    ):
        """Start a validated command, serializing it behind old-goal cancel."""
        previous = {
            "mode": self.mode,
            "goal_type": self.active_goal_type,
            "goal": dict(self.active_goal) if isinstance(self.active_goal, dict) else None,
            "request_id": self.active_request_id,
            "command": self.active_command,
        }
        self.generation += 1
        generation = self.generation
        request_id = self._request_id(command, payload, generation)
        self._cancel_timer()
        self.active_request_id = request_id
        self.active_command = command

        self._publish_navigation(
            request_id=request_id,
            command=command,
            goal_type=goal_type,
            phase="received",
            goal=status_goal,
            message=f"{command} command received.",
            generation=generation,
        )

        old_key = self._active_goal_key
        if old_key is None:
            continuation(generation, request_id, previous, False, "")
            return

        transition = {
            "generation": generation,
            "request_id": request_id,
            "command": command,
            "goal_type": goal_type,
            "status_goal": dict(status_goal) if isinstance(status_goal, dict) else None,
            "previous": previous,
            "old_key": old_key,
            "old_handle": self.current_goal,
            "continuation": continuation,
            "cancel_started": False,
            "cancel_ack": False,
            "result_canceled": False,
        }
        self._pending_transition = transition
        self._publish_navigation(
            request_id=request_id,
            command=command,
            goal_type=goal_type,
            phase="canceling",
            goal=status_goal,
            message="Waiting for Nav2 to confirm cancellation of the previous goal.",
            generation=generation,
        )
        self._arm_cancel_timeout(transition)

        if self.current_goal is not None:
            self._request_transition_cancel(self.current_goal, transition)
        # Otherwise the old send_goal_async response is still pending. Its
        # response callback will route an accepted handle into this transition.

    def _request_transition_cancel(self, handle, transition):
        if self._pending_transition is not transition:
            self._cancel_stale_handle(handle)
            return
        if transition["cancel_started"]:
            return
        transition["cancel_started"] = True
        transition["old_handle"] = handle
        try:
            future = handle.cancel_goal_async()
        except Exception as exc:  # noqa: BLE001
            self._fail_transition(
                transition, f"Nav2 cancellation request failed: {exc}"
            )
            return

        if future is None:
            self._fail_transition(
                transition,
                "Nav2 did not return a cancellation confirmation future.",
            )
            return
        if hasattr(future, "add_done_callback"):
            future.add_done_callback(
                lambda completed: self._on_cancel_response(completed, transition)
            )
            return

        # Non-rclpy test doubles may return a response object synchronously.
        self._handle_cancel_response_value(future, transition)

    @staticmethod
    def _cancel_response_succeeded(response):
        goals_canceling = getattr(response, "goals_canceling", None)
        if goals_canceling is not None:
            return len(goals_canceling) > 0
        return_code = getattr(response, "return_code", None)
        if return_code is not None:
            try:
                return int(return_code) == 0
            except (TypeError, ValueError):
                return False
        success = getattr(response, "success", None)
        if success is not None:
            return bool(success)
        return response is True

    def _on_cancel_response(self, future, transition):
        try:
            response = future.result()
        except Exception as exc:  # noqa: BLE001
            if self._pending_transition is transition:
                self._fail_transition(
                    transition, f"Nav2 cancellation response failed: {exc}"
                )
            return
        self._handle_cancel_response_value(response, transition)

    def _handle_cancel_response_value(self, response, transition):
        succeeded = self._cancel_response_succeeded(response)
        if self._pending_transition is not transition:
            # CancelGoal ACK only accepts the cancel request; it does not prove
            # the action reached terminal CANCELED, so never continue/clear here.
            return
        if not succeeded:
            code = getattr(response, "return_code", None)
            self._fail_transition(
                transition,
                f"Nav2 rejected cancellation (return_code={code!r}).",
            )
            return
        transition["cancel_ack"] = True
        self._publish_navigation(
            request_id=transition["request_id"],
            command=transition["command"],
            goal_type=transition["goal_type"],
            phase="canceling",
            goal=transition["status_goal"],
            message=(
                "Nav2 accepted cancellation; waiting for the old goal's "
                "terminal CANCELED result."
            ),
            generation=transition["generation"],
        )
        if transition["result_canceled"]:
            self._complete_transition(
                transition,
                canceled=True,
                message="Nav2 confirmed the previous goal is canceled.",
            )

    def _complete_transition(self, transition, *, canceled, message):
        if self._pending_transition is not transition:
            return
        self._cancel_cancel_timer()
        self._pending_transition = None
        if canceled:
            self._clear_goal_context(*transition["old_key"])
        transition["continuation"](
            transition["generation"],
            transition["request_id"],
            transition["previous"],
            canceled,
            message,
        )

    def _fail_transition(self, transition, message, *, safe_mode=None):
        if self._pending_transition is not transition:
            return
        self._cancel_cancel_timer()
        self._pending_transition = None
        # Keep the previous mode: cancellation was not confirmed, so claiming
        # idle/paused would be unsafe and could hide continued robot motion.
        self._set_mode(safe_mode or transition["previous"]["mode"])
        self._publish_navigation(
            request_id=transition["request_id"],
            command=transition["command"],
            goal_type=transition["goal_type"],
            phase="failed",
            goal=transition["status_goal"],
            message=message,
            generation=transition["generation"],
        )

    def _handle_superseded_goal_response(
        self, handle, generation, index, goal_type, goal_serial
    ):
        key = (generation, index, goal_type, goal_serial)
        transition = getattr(self, "_pending_transition", None)
        if transition is not None and transition["old_key"] == key:
            if handle is not None and getattr(handle, "accepted", False):
                self.current_goal = handle
                transition["old_handle"] = handle
                try:
                    result_future = handle.get_result_async()
                except Exception as exc:  # noqa: BLE001
                    self._fail_transition(
                        transition,
                        f"Could not track the superseded goal result: {exc}",
                    )
                    return
                result_future.add_done_callback(
                    lambda completed: self._on_transition_old_result(
                        completed, transition
                    )
                )
                self._request_transition_cancel(handle, transition)
            else:
                self._complete_transition(
                    transition,
                    canceled=False,
                    message="The previous goal was not accepted; no cancellation was needed.",
                )
            return
        self._cancel_stale_handle(handle)

    def _on_transition_old_result(self, future, transition):
        try:
            result = future.result()
            status = result.status
        except Exception as exc:  # noqa: BLE001
            if self._pending_transition is transition:
                self._fail_transition(
                    transition, f"Could not read the superseded goal result: {exc}"
                )
            return
        self._handle_transition_old_status(status, transition)

    def _handle_transition_old_status(self, status, transition):
        if self._pending_transition is not transition:
            # A late terminal result after timeout may still safely retire only
            # the old context. It must never execute the abandoned continuation.
            if status in {
                GoalStatus.STATUS_SUCCEEDED,
                GoalStatus.STATUS_ABORTED,
                GoalStatus.STATUS_CANCELED,
            }:
                self._clear_goal_context(*transition["old_key"])
            return

        if status != GoalStatus.STATUS_CANCELED:
            self._clear_goal_context(*transition["old_key"])
            self._fail_transition(
                transition,
                f"Previous goal ended with status {status}, not CANCELED; "
                "the replacement goal was not sent.",
                safe_mode="idle",
            )
            return

        transition["result_canceled"] = True
        self._publish_navigation(
            request_id=transition["request_id"],
            command=transition["command"],
            goal_type=transition["goal_type"],
            phase="canceling",
            goal=transition["status_goal"],
            message=(
                "Old goal reported CANCELED; waiting for CancelGoal confirmation."
                if not transition["cancel_ack"] else
                "Old goal cancellation is fully confirmed."
            ),
            generation=transition["generation"],
        )
        if transition["cancel_ack"]:
            self._complete_transition(
                transition,
                canceled=True,
                message="Nav2 confirmed the previous goal is canceled.",
            )

    def _context_is_current(self, generation, index, goal_type):
        if self.shutting_down or generation != self.generation:
            return False
        if goal_type == "patrol":
            return self.mode == "patrolling" and index == self.index
        if goal_type == "goto":
            return self.mode == "navigating"
        return False

    def _goal_context_is_current(self, generation, index, goal_type, goal_serial):
        return (
            self._context_is_current(generation, index, goal_type)
            and self._active_goal_key
            == (generation, index, goal_type, goal_serial)
        )

    def _clear_goal_context(self, generation, index, goal_type, goal_serial):
        key = (generation, index, goal_type, goal_serial)
        if self._active_goal_key != key:
            return False
        self.current_goal = None
        self._active_goal_key = None
        self.active_goal = None
        self.active_goal_type = None
        return True

    @staticmethod
    def _cancel_stale_handle(handle):
        if handle is None or not getattr(handle, "accepted", False):
            return
        try:
            handle.cancel_goal_async()
        except Exception:  # noqa: BLE001 - stale callbacks must never affect state
            pass

    # ------------------------------------------------------------------
    # Critical-detection hold
    # ------------------------------------------------------------------
    def _on_safety_status(self, msg):
        """Pause an active patrol once for each fire/PPE-violation sighting.

        Empty/non-critical frames must persist for ``critical_rearm_clear_seconds``
        before another hold can be armed.  This prevents a detector publishing at
        camera frame rate from keeping the robot stopped forever on one object.
        """
        labels = critical_labels_from_status(getattr(msg, "data", ""))
        now = time.monotonic()

        if not labels:
            if (
                self._critical_hold_latched
                and self._critical_last_seen is not None
                and now - self._critical_last_seen
                >= self.critical_rearm_clear_seconds
            ):
                self._critical_hold_latched = False
                self._critical_last_seen = None
                self.get_logger().info(
                    "Critical-detection hold re-armed after the scene cleared."
                )
            return

        self._critical_last_seen = now
        if self._critical_hold_latched or self.critical_hold_seconds <= 0.0:
            return

        # This feature belongs only to automatic patrol.  It must not cancel a
        # dashboard goto, override an operator pause/stop, or race another
        # control transition that is already waiting for Nav2 cancellation.
        if self.mode != "patrolling" or self._pending_transition is not None:
            return

        self._critical_hold_latched = True
        self._begin_critical_hold(labels)

    def _begin_critical_hold(self, labels):
        """Cancel the current patrol goal, hold, then resume the same index."""
        prior_goal_type = self.active_goal_type or "patrol"
        prior_goal = (
            dict(self.active_goal) if isinstance(self.active_goal, dict) else None
        )
        label_text = ", ".join(sorted(labels))
        payload = {
            "request_id": f"critical-hold-{time.monotonic_ns()}",
            "reason": label_text,
        }

        def continue_hold(generation, request_id, previous, canceled, message):
            del message
            self._paused_goal_type = previous["goal_type"] or "patrol"
            self._paused_goal = previous["goal"]
            self._set_mode("paused")
            self._critical_hold_request_id = request_id
            self._publish_navigation(
                request_id=request_id,
                command="pause",
                goal_type=prior_goal_type,
                phase="canceled" if canceled else "accepted",
                goal=prior_goal,
                message=(
                    f"Critical detection ({label_text}); patrol held for "
                    f"{self.critical_hold_seconds:.1f}s."
                ),
                generation=generation,
                index=self.index,
            )
            self.get_logger().warning(
                f"Critical detection ({label_text}): patrol stopped for "
                f"{self.critical_hold_seconds:.1f}s."
            )
            self.schedule(
                self.critical_hold_seconds,
                lambda: self._resume_critical_hold(request_id),
                generation=generation,
                index=self.index,
            )

        self._begin_transition(
            "pause",
            payload,
            goal_type=prior_goal_type,
            status_goal=prior_goal,
            continuation=continue_hold,
        )

    def _resume_critical_hold(self, request_id):
        """Resume only if no operator command replaced this automatic hold."""
        if (
            self.mode != "paused"
            or self._critical_hold_request_id != request_id
            or self.active_request_id != request_id
        ):
            return

        self._critical_hold_request_id = None
        msg = String()
        msg.data = json.dumps({
            "command": "resume",
            "request_id": f"{request_id}-resume",
            "reason": "critical-detection hold elapsed",
        })
        self.get_logger().info("Critical-detection hold elapsed; resuming patrol.")
        self._on_control(msg)

    # ------------------------------------------------------------------
    # Route/control input
    # ------------------------------------------------------------------
    def _parse_route(self, payload):
        raw = payload.get('waypoints')
        if not isinstance(raw, list) or not raw:
            self.get_logger().error("The start command does not contain waypoints.")
            return None

        parsed = []
        for item in raw:
            try:
                waypoint = (
                    float(item['x']),
                    float(item['y']),
                    float(item.get('yaw', 0.0)),
                )
            except (KeyError, TypeError, ValueError):
                self.get_logger().error(f"Invalid patrol waypoint: {item!r}")
                return None
            if not all(math.isfinite(value) for value in waypoint):
                self.get_logger().error(f"Non-finite patrol waypoint: {item!r}")
                return None
            parsed.append(waypoint)
        return parsed

    def _commit_route(self, parsed, payload):
        self.waypoints = parsed
        self.loop = bool(payload.get('loop', True))
        self.route_source = str(payload.get('source', 'dashboard'))
        self.index = 0
        self.retries = 0
        self.consecutive_failures = 0
        # Compatibility for the planner-to-node contract test and older code
        # which applies a route before setting the explicit mode.
        self.paused = False
        self._publish_route()
        self.get_logger().info(
            f"Loaded {len(parsed)} patrol waypoints from {self.route_source}."
        )

    def _apply_route(self, payload):
        """Validate and install a route (legacy/direct-call compatibility)."""
        parsed = self._parse_route(payload)
        if parsed is None:
            return False
        self._commit_route(parsed, payload)
        return True

    @staticmethod
    def _parse_goto(payload):
        try:
            x = float(payload['x'])
            y = float(payload['y'])
            yaw = float(payload.get('yaw', 0.0))
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in (x, y, yaw)):
            return None
        return {"x": x, "y": y, "yaw": yaw}

    def _make_stop_continuation(self, command, prior_goal_type, prior_goal):
        def continue_stop(generation, request_id, previous, canceled, message):
            del previous, message
            self.index = 0
            self.retries = 0
            self.consecutive_failures = 0
            self._paused_goal_type = None
            self._paused_goal = None
            self._set_mode("idle")
            self._publish_navigation(
                request_id=request_id,
                command=command,
                goal_type=prior_goal_type,
                phase="canceled" if canceled else "accepted",
                goal=prior_goal,
                message=(
                    "Navigation stopped after Nav2 confirmed cancellation."
                    if canceled else
                    "Navigation was already inactive; patrol index reset."
                ),
                generation=generation,
            )
            self.get_logger().info("Navigation stopped.")

        return continue_stop

    def _supersede_pending_with_stop(self, payload):
        """Give stop priority without issuing a second cancellation request."""
        transition = self._pending_transition
        if transition is None:
            return False

        supplied_request_id = payload.get("request_id")
        if supplied_request_id is not None:
            supplied_request_id = str(supplied_request_id)
        if transition["command"] == "stop" and (
            not supplied_request_id
            or supplied_request_id == transition["request_id"]
        ):
            self._publish_navigation(
                request_id=transition["request_id"],
                command="stop",
                goal_type=transition["goal_type"],
                phase="canceling",
                goal=transition["status_goal"],
                message="The stop command is still waiting for final cancellation.",
                generation=transition["generation"],
            )
            return True

        # Terminate the displaced request explicitly so its waiter does not
        # mistake the later stop status for its own completion.
        self._publish_navigation(
            request_id=transition["request_id"],
            command=transition["command"],
            goal_type=transition["goal_type"],
            phase="failed",
            goal=transition["status_goal"],
            message="Navigation replacement was superseded by an operator stop command.",
            generation=transition["generation"],
        )

        self.generation += 1
        generation = self.generation
        request_id = self._request_id("stop", payload, generation)
        previous = transition["previous"]
        prior_goal_type = previous["goal_type"] or (
            "patrol" if previous["mode"] == "patrolling"
            else "goto" if previous["mode"] == "navigating"
            else "control"
        )
        prior_goal = previous["goal"]

        transition["generation"] = generation
        transition["request_id"] = request_id
        transition["command"] = "stop"
        transition["goal_type"] = prior_goal_type
        transition["status_goal"] = prior_goal
        transition["continuation"] = self._make_stop_continuation(
            "stop", prior_goal_type, prior_goal
        )
        self.active_request_id = request_id
        self.active_command = "stop"

        self._publish_navigation(
            request_id=request_id,
            command="stop",
            goal_type=prior_goal_type,
            phase="received",
            goal=prior_goal,
            message="stop command received; taking over the pending cancellation.",
            generation=generation,
        )
        self._publish_navigation(
            request_id=request_id,
            command="stop",
            goal_type=prior_goal_type,
            phase="canceling",
            goal=prior_goal,
            message="Waiting for final confirmation that the old goal is canceled.",
            generation=generation,
        )
        self._arm_cancel_timeout(transition)
        if transition["cancel_ack"] and transition["result_canceled"]:
            self._complete_transition(
                transition,
                canceled=True,
                message="Nav2 confirmed the previous goal is canceled.",
            )
        return True

    def _on_control(self, msg):
        try:
            payload = json.loads(msg.data)
            if not isinstance(payload, dict):
                raise ValueError
            command = str(payload.get('command', '')).strip().lower()
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
            command = str(msg.data).strip().lower()

        if command not in {"start", "stop", "pause", "resume", "goto"}:
            self.get_logger().warning(f"Unknown patrol control command: {msg.data!r}")
            return

        supplied_request_id = payload.get("request_id")
        if supplied_request_id is not None:
            supplied_request_id = str(supplied_request_id)
        if (
            supplied_request_id
            and supplied_request_id == self.active_request_id
            and supplied_request_id == self.navigation.get("request_id")
            and command == self.active_command
        ):
            # HTTP retries with the same request_id are idempotent: repeat the
            # current acknowledgement instead of canceling/restarting motion.
            replay = String()
            replay.data = json.dumps(self.navigation)
            self.navigation_status_pub.publish(replay)
            return

        # Validate everything before changing generation, timers or active
        # goals. A malformed/invalid command must be a true no-op for motion.
        parsed_route = None
        start_points = self.waypoints
        requested_goto = None
        resume_type = None
        resume_goal = None

        if command == 'start':
            if "waypoints" in payload:
                parsed_route = self._parse_route(payload)
                if parsed_route is None:
                    self._reject_command(
                        command, payload, "patrol", None,
                        "A non-empty, valid waypoint list is required.",
                    )
                    return
                start_points = parsed_route
            if not start_points:
                self._reject_command(
                    command, payload, "patrol", None,
                    "No patrol route is available.",
                )
                return

        elif command == "goto":
            requested_goto = self._parse_goto(payload)
            if requested_goto is None:
                self._reject_command(
                    command, payload, "goto", None,
                    "goto requires finite x, y and optional yaw values.",
                )
                return

        elif command == "resume":
            if self.mode != "paused":
                self._reject_command(
                    command, payload, "control", None,
                    "resume is only valid after a confirmed pause command.",
                )
                return
            resume_type = self._paused_goal_type
            resume_goal = self._paused_goal
            if resume_type == "goto" and not isinstance(resume_goal, dict):
                self._reject_command(
                    command, payload, "goto", None,
                    "There is no saved point-navigation goal to resume.",
                )
                return
            if resume_type == "patrol" and not self.waypoints:
                self._reject_command(
                    command, payload, "patrol", None,
                    "There is no patrol route to resume.",
                )
                return
            if resume_type not in {"goto", "patrol"}:
                self._reject_command(
                    command, payload, "control", None,
                    "There is no paused navigation operation to resume.",
                )
                return

        # A command already waiting for cancellation confirmation owns this
        # transition. Rejecting another command avoids duplicate cancel calls
        # and keeps its continuation deterministic.
        if self._pending_transition is not None:
            if command == "stop":
                self._supersede_pending_with_stop(payload)
                return
            self._reject_command(
                command,
                payload,
                "goto" if command == "goto" else "control",
                requested_goto,
                "Another navigation command is waiting for cancellation confirmation.",
            )
            return

        prior_goal_type = self.active_goal_type or (
            "patrol" if self.mode == "patrolling"
            else "goto" if self.mode == "navigating"
            else "control"
        )
        prior_goal = (
            dict(self.active_goal) if isinstance(self.active_goal, dict) else None
        )

        if command == 'start':
            first = start_points[0]
            start_goal = self._goal_dict(first[0], first[1], first[2])

            def continue_start(generation, request_id, previous, canceled, message):
                del previous, canceled, message
                if parsed_route is not None:
                    self._commit_route(parsed_route, payload)
                self.index = 0
                self.retries = 0
                self.consecutive_failures = 0
                self._paused_goal_type = None
                self._paused_goal = None
                self._set_mode("patrolling")
                self.get_logger().info("Starting patrol.")
                self.send_goal(generation=generation, index=self.index)

            self._begin_transition(
                command,
                payload,
                goal_type="patrol",
                status_goal=start_goal,
                continuation=continue_start,
            )
            return

        if command == 'stop':
            self._begin_transition(
                command,
                payload,
                goal_type=prior_goal_type,
                status_goal=prior_goal,
                continuation=self._make_stop_continuation(
                    command, prior_goal_type, prior_goal
                ),
            )
            return

        if command == "pause":
            def continue_pause(generation, request_id, previous, canceled, message):
                del message
                if previous["mode"] != "paused":
                    self._paused_goal_type = previous["goal_type"] or (
                        "patrol" if previous["mode"] == "patrolling" else None
                    )
                    self._paused_goal = previous["goal"]
                self._set_mode("paused")
                self._publish_navigation(
                    request_id=request_id,
                    command=command,
                    goal_type=prior_goal_type,
                    phase="canceled" if canceled else "accepted",
                    goal=prior_goal,
                    message=(
                        "Navigation paused after Nav2 confirmed cancellation."
                        if canceled else
                        "Navigation paused; there was no active Nav2 goal."
                    ),
                    generation=generation,
                )
                self.get_logger().info("Navigation paused.")

            self._begin_transition(
                command,
                payload,
                goal_type=prior_goal_type,
                status_goal=prior_goal,
                continuation=continue_pause,
            )
            return

        if command == "resume":
            target_goal = resume_goal
            if resume_type == "patrol":
                resume_index = self.index if self.index < len(self.waypoints) else 0
                waypoint = self.waypoints[resume_index]
                target_goal = self._goal_dict(*waypoint)

            def continue_resume(generation, request_id, previous, canceled, message):
                del previous, canceled, message
                self._paused_goal_type = None
                self._paused_goal = None
                if resume_type == "goto":
                    self._set_mode("navigating")
                    self._send_navigation_goal(
                        resume_goal,
                        goal_type="goto",
                        generation=generation,
                        index=None,
                        request_id=request_id,
                        command=command,
                    )
                    return
                if self.index >= len(self.waypoints):
                    self.index = 0
                self._set_mode("patrolling")
                self.get_logger().info("Resuming patrol.")
                self.send_goal(generation=generation, index=self.index)

            self._begin_transition(
                command,
                payload,
                goal_type=resume_type,
                status_goal=target_goal,
                continuation=continue_resume,
            )
            return

        # goto
        def continue_goto(generation, request_id, previous, canceled, message):
            del previous, canceled, message
            self._paused_goal_type = None
            self._paused_goal = None
            self._set_mode("navigating")
            self._send_navigation_goal(
                requested_goto,
                goal_type="goto",
                generation=generation,
                index=None,
                request_id=request_id,
                command=command,
            )

        self._begin_transition(
            command,
            payload,
            goal_type="goto",
            status_goal=requested_goto,
            continuation=continue_goto,
        )

    # ------------------------------------------------------------------
    # Nav2 action lifecycle
    # ------------------------------------------------------------------
    def make_pose(self, x, y, yaw):
        pose = PoseStamped()
        pose.header.frame_id = 'map'
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.pose.position.x = float(x)
        pose.pose.position.y = float(y)
        pose.pose.position.z = 0.0
        pose.pose.orientation.z = math.sin(float(yaw) / 2.0)
        pose.pose.orientation.w = math.cos(float(yaw) / 2.0)
        return pose

    def send_goal(self, generation=None, index=None):
        """Send the current patrol waypoint with captured callback context."""
        if self.shutting_down or not self.waypoints or self.mode != "patrolling":
            return
        generation = self.generation if generation is None else generation
        index = self.index if index is None else index
        if not self._context_is_current(generation, index, "patrol"):
            return

        if index >= len(self.waypoints):
            if self.loop:
                self.get_logger().info("Patrol loop complete; returning to waypoint 1.")
                self.index = 0
                index = 0
            else:
                self._set_mode("idle")
                self._publish_navigation(
                    request_id=self.active_request_id,
                    command=self.active_command or "start",
                    goal_type="patrol",
                    phase="succeeded",
                    goal=None,
                    message="Patrol route completed.",
                    generation=generation,
                )
                return

        x, y, yaw = self.waypoints[index]
        goal = self._goal_dict(x, y, yaw)
        attempt = "" if self.retries == 0 else f" retry {self.retries}/{self.max_retry}"
        self.get_logger().info(
            f"Navigating to patrol waypoint {index + 1}/{len(self.waypoints)}: "
            f"({x}, {y}), yaw={yaw}{attempt}"
        )
        self._send_navigation_goal(
            goal,
            goal_type="patrol",
            generation=generation,
            index=index,
            request_id=self.active_request_id,
            command=self.active_command or "start",
        )

    def _send_navigation_goal(
        self,
        goal,
        *,
        goal_type,
        generation,
        index,
        request_id,
        command,
    ):
        if not self._context_is_current(generation, index, goal_type):
            return

        goal = dict(goal)
        self.active_goal = goal
        self.active_goal_type = goal_type
        self.active_request_id = request_id
        self.active_command = command
        self.distance_remaining = None
        self.goal_serial += 1
        goal_serial = self.goal_serial
        self._active_goal_key = (generation, index, goal_type, goal_serial)
        self._publish_navigation(
            request_id=request_id,
            command=command,
            goal_type=goal_type,
            phase="received",
            goal=goal,
            message="Navigation goal sent to Nav2.",
            generation=generation,
            index=index,
        )

        nav_goal = NavigateToPose.Goal()
        nav_goal.pose = self.make_pose(goal["x"], goal["y"], goal["yaw"])
        try:
            future = self.nav_client.send_goal_async(
                nav_goal,
                feedback_callback=lambda feedback: self.on_feedback(
                    feedback,
                    generation,
                    index,
                    goal_type,
                    request_id,
                    command,
                    goal,
                    goal_serial,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self._handle_goal_error(
                phase="failed",
                message=f"Could not send navigation goal: {exc}",
                generation=generation,
                index=index,
                goal_type=goal_type,
                request_id=request_id,
                command=command,
                goal=goal,
                goal_serial=goal_serial,
            )
            return

        future.add_done_callback(
            lambda completed: self.on_goal_response(
                completed,
                generation,
                index,
                goal_type,
                request_id,
                command,
                goal,
                goal_serial,
            )
        )

    def on_goal_response(
        self,
        future,
        generation=None,
        index=None,
        goal_type=None,
        request_id=None,
        command=None,
        goal=None,
        goal_serial=None,
    ):
        # Optional defaults keep this method convenient in small ROS stubs and
        # preserve compatibility with code that called on_goal_response(future).
        generation = self.generation if generation is None else generation
        goal_type = goal_type or self.active_goal_type or "patrol"
        if goal_type == "patrol" and index is None:
            index = self.index
        request_id = request_id or self.active_request_id
        command = command or self.active_command or ("goto" if goal_type == "goto" else "start")
        goal = dict(goal or self.active_goal or {}) or None
        goal_serial = getattr(self, "goal_serial", 0) if goal_serial is None else goal_serial

        try:
            handle = future.result()
        except Exception as exc:  # noqa: BLE001
            transition = self._pending_transition
            key = (generation, index, goal_type, goal_serial)
            if transition is not None and transition["old_key"] == key:
                self._fail_transition(
                    transition,
                    f"Superseded goal response failed before safe cancellation: {exc}",
                )
                return
            if self._goal_context_is_current(
                generation, index, goal_type, goal_serial
            ):
                self._handle_goal_error(
                    phase="failed",
                    message=f"Nav2 goal response failed: {exc}",
                    generation=generation,
                    index=index,
                    goal_type=goal_type,
                    request_id=request_id,
                    command=command,
                    goal=goal,
                    goal_serial=goal_serial,
                )
            return

        if not self._goal_context_is_current(
            generation, index, goal_type, goal_serial
        ):
            # A pause/stop/goto can arrive while Nav2 is deciding whether to
            # accept the old goal. Cancel it as soon as its handle appears.
            self._handle_superseded_goal_response(
                handle, generation, index, goal_type, goal_serial
            )
            return

        if handle is None or not handle.accepted:
            self._handle_goal_error(
                phase="rejected",
                message="Nav2 rejected the navigation goal.",
                generation=generation,
                index=index,
                goal_type=goal_type,
                request_id=request_id,
                command=command,
                goal=goal,
                goal_serial=goal_serial,
            )
            return

        self.current_goal = handle
        self._active_goal_key = (generation, index, goal_type, goal_serial)
        self._publish_navigation(
            request_id=request_id,
            command=command,
            goal_type=goal_type,
            phase="accepted",
            goal=goal,
            message="Nav2 accepted the navigation goal.",
            generation=generation,
            index=index,
        )
        try:
            result_future = handle.get_result_async()
        except Exception as exc:  # noqa: BLE001
            self._handle_goal_error(
                phase="failed",
                message=f"Could not wait for the Nav2 result: {exc}",
                generation=generation,
                index=index,
                goal_type=goal_type,
                request_id=request_id,
                command=command,
                goal=goal,
                goal_serial=goal_serial,
            )
            return
        result_future.add_done_callback(
            lambda completed: self.on_result(
                completed,
                generation,
                index,
                goal_type,
                request_id,
                command,
                goal,
                goal_serial,
            )
        )

    def on_feedback(
        self,
        feedback_msg,
        generation=None,
        index=None,
        goal_type=None,
        request_id=None,
        command=None,
        goal=None,
        goal_serial=None,
    ):
        generation = self.generation if generation is None else generation
        goal_type = goal_type or self.active_goal_type or "patrol"
        if goal_type == "patrol" and index is None:
            index = self.index
        goal_serial = getattr(self, "goal_serial", 0) if goal_serial is None else goal_serial
        if not self._goal_context_is_current(
            generation, index, goal_type, goal_serial
        ):
            return

        try:
            remaining = float(feedback_msg.feedback.distance_remaining)
        except (AttributeError, TypeError, ValueError):
            return
        if not math.isfinite(remaining):
            return

        self._publish_navigation(
            request_id=request_id or self.active_request_id,
            command=command or self.active_command or "start",
            goal_type=goal_type,
            phase="accepted",
            goal=goal or self.active_goal,
            message="Navigation is in progress.",
            generation=generation,
            index=index,
            distance_remaining=remaining,
            # /patrol_status already publishes at 1 Hz. Avoid re-sending the
            # latched route at the Nav2 feedback frequency.
            refresh_embedded=False,
        )

    def on_result(
        self,
        future,
        generation=None,
        index=None,
        goal_type=None,
        request_id=None,
        command=None,
        goal=None,
        goal_serial=None,
    ):
        generation = self.generation if generation is None else generation
        goal_type = goal_type or self.active_goal_type or "patrol"
        if goal_type == "patrol" and index is None:
            index = self.index
        request_id = request_id or self.active_request_id
        command = command or self.active_command or ("goto" if goal_type == "goto" else "start")
        goal = dict(goal or self.active_goal or {}) or None
        goal_serial = getattr(self, "goal_serial", 0) if goal_serial is None else goal_serial

        if not self._goal_context_is_current(
            generation, index, goal_type, goal_serial
        ):
            transition = self._pending_transition
            key = (generation, index, goal_type, goal_serial)
            if transition is not None and transition["old_key"] == key:
                self._on_transition_old_result(future, transition)
            elif self._active_goal_key == key:
                # A transition may already have failed/timed out. Retire the
                # stale context when its terminal result eventually arrives,
                # without overwriting the newer command's failed status.
                try:
                    stale_status = future.result().status
                except Exception:  # noqa: BLE001
                    return
                if stale_status in {
                    GoalStatus.STATUS_SUCCEEDED,
                    GoalStatus.STATUS_ABORTED,
                    GoalStatus.STATUS_CANCELED,
                }:
                    self._clear_goal_context(*key)
                    self._set_mode("idle")
                    self.navigation["state"] = self.mode
                    self._publish_status()
            return

        try:
            result = future.result()
            status = result.status
        except Exception as exc:  # noqa: BLE001
            self._handle_goal_error(
                phase="failed",
                message=f"Could not read the Nav2 result: {exc}",
                generation=generation,
                index=index,
                goal_type=goal_type,
                request_id=request_id,
                command=command,
                goal=goal,
                goal_serial=goal_serial,
            )
            return

        if status == GoalStatus.STATUS_SUCCEEDED:
            self._clear_goal_context(
                generation, index, goal_type, goal_serial
            )
            if goal_type == "goto":
                self._set_mode("idle")
                self._publish_navigation(
                    request_id=request_id,
                    command=command,
                    goal_type=goal_type,
                    phase="succeeded",
                    goal=goal,
                    message="The robot reached the requested location.",
                    generation=generation,
                    distance_remaining=0.0,
                )
                self.get_logger().info("Dashboard navigation goal reached.")
                return

            self.consecutive_failures = 0
            self.retries = 0
            self._publish_navigation(
                request_id=request_id,
                command=command,
                goal_type=goal_type,
                phase="succeeded",
                goal=goal,
                message=f"Patrol waypoint {index + 1} reached.",
                generation=generation,
                index=index,
                distance_remaining=0.0,
            )
            self.get_logger().info(
                f"Patrol waypoint {index + 1} reached; dwelling for {self.dwell:.1f}s."
            )
            self.schedule(
                self.dwell,
                lambda: self.advance(generation=generation, index=index),
                generation=generation,
                index=index,
            )
            return

        if status == GoalStatus.STATUS_CANCELED:
            self._clear_goal_context(
                generation, index, goal_type, goal_serial
            )
            if goal_type == "goto":
                self._set_mode("idle")
            self._publish_navigation(
                request_id=request_id,
                command=command,
                goal_type=goal_type,
                phase="canceled",
                goal=goal,
                message="Nav2 canceled the active navigation goal.",
                generation=generation,
                index=index,
            )
            if goal_type == "goto":
                return
            else:
                # Explicit command cancellations have a newer generation and
                # never reach here. A same-generation cancellation is
                # unexpected, so retry it instead of leaving mode=patrolling
                # with no active goal.
                self.after_failure(generation=generation, index=index)
            return

        status_name = {
            GoalStatus.STATUS_ABORTED: "ABORTED",
        }.get(status, f"status {status}")
        self._handle_goal_error(
            phase="failed",
            message=f"Navigation failed with {status_name}.",
            generation=generation,
            index=index,
            goal_type=goal_type,
            request_id=request_id,
            command=command,
            goal=goal,
            goal_serial=goal_serial,
        )

    def _handle_goal_error(
        self,
        *,
        phase,
        message,
        generation,
        index,
        goal_type,
        request_id,
        command,
        goal,
        goal_serial,
    ):
        if not self._goal_context_is_current(
            generation, index, goal_type, goal_serial
        ):
            return
        self._clear_goal_context(generation, index, goal_type, goal_serial)

        if goal_type == "goto":
            self._set_mode("idle")
        self._publish_navigation(
            request_id=request_id,
            command=command,
            goal_type=goal_type,
            phase=phase,
            goal=goal,
            message=message,
            generation=generation,
            index=index,
        )
        if phase == "rejected":
            self.get_logger().warning(message)
        else:
            self.get_logger().error(message)

        if goal_type == "patrol":
            self.after_failure(generation=generation, index=index)

    # ------------------------------------------------------------------
    # Patrol retry/advance behaviour
    # ------------------------------------------------------------------
    def after_failure(self, generation=None, index=None):
        generation = self.generation if generation is None else generation
        index = self.index if index is None else index
        if not self._context_is_current(generation, index, "patrol"):
            return
        if not self.waypoints:
            return

        self.consecutive_failures += 1
        if self.consecutive_failures == len(self.waypoints):
            self.get_logger().error(
                "Every patrol waypoint has failed consecutively. Check the map, "
                "waypoint bounds, localization and Nav2 costmaps."
            )

        if self.retries < self.max_retry:
            self.retries += 1
            self.schedule(
                self.retry_delay,
                lambda: self.send_goal(generation=generation, index=index),
                generation=generation,
                index=index,
            )
            return

        self.get_logger().warning(
            f"Skipping patrol waypoint {index + 1} after {self.max_retry} retries."
        )
        self.retries = 0
        self.schedule(
            self.retry_delay,
            lambda: self.advance(generation=generation, index=index),
            generation=generation,
            index=index,
        )

    def advance(self, generation=None, index=None):
        generation = self.generation if generation is None else generation
        index = self.index if index is None else index
        if not self._context_is_current(generation, index, "patrol"):
            return
        self.index = index + 1
        self.retries = 0
        self.send_goal(generation=generation, index=self.index)


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = PatrolNode()
        rclpy.spin(node)
    except (SystemExit, KeyboardInterrupt):
        pass
    except Exception:
        # launch 프로세스 그룹 종료 시 ROS context가 먼저 내려가면
        # Humble의 feedback callback publish가 RCLError를 던질 수 있다.
        if rclpy.ok():
            raise
    finally:
        if node is not None:
            node.shutting_down = True
            node.generation += 1
            node._cancel_timer()
            node._cancel_cancel_timer()
            node._pending_transition = None
            node._cancel_current_goal()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
