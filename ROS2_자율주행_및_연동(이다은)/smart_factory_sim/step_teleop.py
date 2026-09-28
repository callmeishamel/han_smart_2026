#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
import sys
import select
import termios
import tty
import time

msg = """
Control Your TurtleBot3 (Step-by-Step Mode)
---------------------------
Moving around:
        w
   a    s    d

w/s : move forward/backward by a fixed step (~0.2m)
a/d : turn left/right by a fixed angle (~28 degrees)

x : force stop immediately

CTRL-C to quit
"""

class StepTeleop(Node):
    def __init__(self):
        super().__init__('step_teleop')
        self.publisher_ = self.create_publisher(Twist, 'cmd_vel', 10)
        
        # Step parameters
        self.linear_vel = 0.2  # m/s
        self.angular_vel = 0.5 # rad/s
        self.step_duration = 1.0 # seconds to apply the velocity
        self.publish_rate = 10 # Hz
        
        self.get_logger().info('Step Teleop Node Started.')

    def publish_step(self, linear, angular):
        twist = Twist()
        twist.linear.x = float(linear)
        twist.angular.z = float(angular)
        
        loops = int(self.step_duration * self.publish_rate)
        sleep_time = 1.0 / self.publish_rate
        
        # Publish for the duration
        for _ in range(loops):
            self.publisher_.publish(twist)
            time.sleep(sleep_time)
            
        # Stop after the step
        self.stop_robot()

    def stop_robot(self):
        twist = Twist()
        twist.linear.x = 0.0
        twist.angular.z = 0.0
        self.publisher_.publish(twist)

def get_key(settings):
    tty.setraw(sys.stdin.fileno())
    rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
    if rlist:
        key = sys.stdin.read(1)
    else:
        key = ''
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)
    return key

def main(args=None):
    settings = termios.tcgetattr(sys.stdin)
    
    rclpy.init(args=args)
    node = StepTeleop()

    print(msg)

    try:
        while rclpy.ok():
            key = get_key(settings)
            
            if key == 'w':
                print("Step Forward")
                node.publish_step(node.linear_vel, 0.0)
            elif key == 's':
                print("Step Backward")
                node.publish_step(-node.linear_vel, 0.0)
            elif key == 'a':
                print("Step Left")
                node.publish_step(0.0, node.angular_vel)
            elif key == 'd':
                print("Step Right")
                node.publish_step(0.0, -node.angular_vel)
            elif key == 'x':
                print("Force Stop")
                node.stop_robot()
            elif key == '\x03': # CTRL-C
                break
                
    except Exception as e:
        print(e)
    finally:
        node.stop_robot()
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == '__main__':
    main()
