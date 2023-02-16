#!/usr/bin/env python3
""" Mission Manager Node

Subscribes:

* "project11/piloting_mode" with String - used to change piloting mode.
* "odom" with Odometry
* "project11/mission_manager/command" with String - See commands notes below.
* "project11/heartbeat" with Heartbeat - can also be used to change piloting mode.

Publishes:

* "project11/status/mission_manager" with Heartbeat - includes a number of key/value pairs to describe the current status of the `state machine and MissionManagerCore object.

Dynamic Reconfiguration:

Uses reconfiguration server for parameters - see mission_manager/cfg 

TODO: Create a Task class, as an alternative to an unconstrained dictionary, 
to define the attributes and methods of an object. 

"""

import rospy
import smach
import smach_ros

from std_msgs.msg import String
from project11_msgs.msg import Heartbeat
from project11_msgs.msg import KeyValue
from geographic_msgs.msg import GeoPoseStamped
from geographic_msgs.msg import GeoPose
from geographic_msgs.msg import GeoPoint
from geographic_visualization_msgs.msg import GeoVizItem, GeoVizPointList

from dynamic_reconfigure.server import Server
from mission_manager.cfg import mission_managerConfig

from dubins_curves.srv import DubinsCurvesLatLong
from dubins_curves.srv import DubinsCurvesLatLongRequest

from nav_msgs.srv import GetPlan
from nav_msgs.srv import GetPlanRequest

import actionlib
import path_follower.msg
import path_planner.msg
import hover.msg
import manda_coverage.msg
import move_base_msgs.msg

import project11

from tf.transformations import quaternion_from_euler

import json
import math

def parseLatLong(args):
    """ Splits a string into latitude and longitude.

    Splits a string in two and crates a dictionary with 
    latitude and longitude keys and float values.

    Args:
        args: 
        A str of two float numbers separated by whitespace.  
    
    Returns:
        A dict with keys 'latitude' and 'longitude' and float values.
    """
    latlon = args.split()
    if len(latlon) == 2:
        try:
            lat = float(latlon[0])
            lon = float(latlon[1])
            return {'latitude':lat, 'longitude':lon}
        except ValueError:
            rospy.logerr("mission_manager: Cannot convert the command "
                            "arguments <%s> into two floats!"%args)
            return None
    else:
        rospy.logerr("mission_manager: Cannot split the command "
                        "arguments <%s> into exactly two elements!"%args)

    return None

def parseMission(plan, default_speed):
    """ Create a task dict from a json description.
    
    Called when a "mission_plan" command is received.

    TODO: The interface changed - need to check documentation.
    
    Args:
        plan: A str in json format describing a mission. 
        default_speed: A float speed in m/s.

    Returns:
        A dict describing the task dictionary for mission 
        described by the plan json.
    """
    ret = []
    speed = default_speed
    
    for item in plan:
        rospy.loginfo("mission_manager: Parsing new mission item <%s>"%item)
        if item['type'] == 'Platform':
            speed = item['speed']*0.514444  # knots to m/s
        if item['type'] in ('SurveyPattern', 'TrackLine', 'SurveyArea'):
            current_item = {'type':'mission_plan',
                'nav_objectives':[],
                'default_speed':speed,
                'do_transit':True,
                'current_nav_objective_index':0
                }
            if item['type'] == 'SurveyPattern':
                for c in item['children']:
                    current_item['nav_objectives'].append(c)
                current_item['label'] = item['label']
            if item['type'] == 'TrackLine':
                current_item['nav_objectives'].append(item)
                current_item['label'] = item['label']
            if item['type'] == 'SurveyArea':
                for c in item['children']:
                    if c['type'] != 'Waypoint':
                        current_item = None
                        break
                if current_item is None:
                    ret += parseMission(item['children'], speed)
                else:
                    current_item['nav_objectives'].append(item)
                    current_item['label'] = item['label']
            if current_item is not None:
                ret.append(current_item)
        if item['type'] == 'Group':
            group_items = parseMission(item['children'], speed)
            ret += group_items

    return ret



class MissionManagerCore(object):
    """Singleton class instantiated by main.

    TODO: Add attribute descriptions for key public attributes.

    Attributes:
        robot_nav: Object providing position and map transforms

    """
    def __init__(self):
        """ Inits MissionManager Core.
        
        * Initializes task accounting attributes.
        * Creates subscribers.
        * Initiates dynamic reconfiguration
        * Starts tf2 transform listener.
        """
        self.piloting_mode = 'standby'

        """ 
        List of tasks to do, or already done.
        Keeping all the tasks allows us to run them in a loop
        Elements of the list are dictionary objects - see addTask()
        """
        self.tasks = []
        # List of tasks to be done. Once a task is completed,
        # it is dropped from this list. Overrides get prepended here.
        #self.pending_tasks = [] 
        self.current_task = None
        # A task that may be added, such as hover,
        # to temporarily interupt current tast.
        self.override_task = None
        # A task that was current when an override task was added
        self.saved_task = None 
        self.pending_command = None

        self.lineup_distance = 25

        self.done_behavior = 'hover'
        
        rospy.Subscriber('project11/piloting_mode', String,
                         self.pilotingModeCallback, queue_size = 1)
        command_topic = 'project11/mission_manager/command' 
        rospy.Subscriber(command_topic, String,
                         self.commandCallback,
                         callback_args = command_topic,
                         queue_size = 1)
        rospy.Subscriber('project11/heartbeat', Heartbeat,
                         self.heartbeatCallback, queue_size = 1)

        self.status_publisher = rospy.Publisher(
            'project11/status/mission_manager',
            Heartbeat, queue_size = 10)
        self.endofline_publisher = rospy.Publisher('project11/endofline',
                                                   String, queue_size = 1)
        self.display_publisher = rospy.Publisher('project11/display',
                                                 GeoVizItem, queue_size = 1)

        # Dynamic reconfiguration server.
        self.config_server = Server(mission_managerConfig,
                                    self.reconfigure_callback)



        self.robot_nav = project11.nav.RobotNavigation()


    def pilotingModeCallback(self, msg):
        """ Called by ROS subscriber to hange piloting_mode string.

        Args:
          msg: A std_msgs/String 
        """
        self.piloting_mode = msg.data

    def heartbeatCallback(self, msg):
        """ Called by ROS subscriber to change piloting_mode strong.

        Note that this can also be done throught he pilotingModeCallback.

        Args: 
          msg: A project11_msgs/Heartbeat message.
        """
        for kv in msg.values:
            if kv.key == 'piloting_mode':
                self.piloting_mode = kv.value
            
    def reconfigure_callback(self, config, level):
        """ Changes configuration attributes dynamic reconfiguration.

        Args:
          config: 
            Dictionary with keys and values described in the 
            cfg/mission_manager.cfg file.
          level: 
            A bitmask which will later be passed to the dynamic reconfigure 
            callback. When the callback is called all of the level values 
            for parameters that have been changed are ORed together and 
            the resulting value is passed to the callback.
            http://wiki.ros.org/dynamic_reconfigure/Tutorials/HowToWriteYourFirstCfgFile
        """
        self.waypointThreshold = config['waypoint_threshold']
        self.turnRadius = config['turn_radius']
        self.segmentLength = config['segment_length']
        self.default_speed = config['default_speed']
        if config['planner'] == 0:
            self.planner = 'path_follower'
        elif config['planner'] == 1:
            self.planner = 'path_planner'
        if config['done_behavior'] == 0:
            self.done_behavior = 'hover'
        elif config['done_behavior'] == 1:
            self.done_behavior = 'restart'
            
        return config
        
    def getPilotingMode(self):
        """ Access method for piloting_mode attribute. 

        TODO:
        Seems not terribly useful since Python doesn't have private attributes.
        Why not just access attribute of the class?

        Returns:
          The piloting_mode attribute str.
        """
        return self.piloting_mode
    
    def commandCallback(self, msg, args):
        """ Receives ROS command String.

        Args:
          msg: 
            A std_msg/String message.
            Formated string, delimited by whitespace, describing task_type 
            and task parameters.
          args: 
            A str, using callback_args functionality of subscriber to 
            pass topic name.
        """

        rospy.loginfo("mission_manager: Received command string <%s>"
                      "on topic <%s>"%(str(msg.data), args))
        
        parts = msg.data.split(None,1)
        cmd = parts[0]
        if len(parts) > 1:
            args = parts[1]
        else:
            args = None
                
        if cmd == 'replace_task':
            self.clearTasks()
            self.addTask(args)
            self.pending_command = 'next_task'
        elif cmd == 'append_task':
            self.addTask(args)
        elif cmd == 'prepend_task':
            self.addTask(args, True)
        elif cmd == 'clear_tasks':
            self.clearTasks()
        elif cmd in ('next_task','prev_task','goto_task',
                   'goto_line', 'start_line', 'restart_mission'):
            self.pending_command  = msg.data
        elif cmd == 'override':
            parts = args.split(None,1)
            if len(parts) == 2:
                task_type = parts[0]   
                task = None
                if task_type == 'goto':
                    task = parseLatLong(parts[1])
                    if task is not None:
                        task['type'] = 'goto'
                if task_type == 'hover':
                    task = parseLatLong(parts[1])
                    if task is not None:
                        task['type'] = 'hover'
                if task is not None:
                    self.setOverride(task)
        else:
            rospy.logerr("mission_manager: No defined action for the "
                         "received command <%s> - ignoring!"%msg.data)
        

    def clearTasks(self):
        """ Empties the tasks list and sets current_task to None.

        Called with a command of "clear_task" or "replace_task" is received.
        """
        self.tasks = []
        self.current_task = None
        self.saved_task = None

    def addTask(self, args, prepend=False):
        """ Appends or prepends an element to the "tasks" list attribute.

        Called when "append_task" or "prepend_task" commands are received.

        Tasks are dictionaries with a variety of keys.  Each dictionary 
        includes a 'type' key.

        Args:
          args: 
            A str that is the task definition string (see README.md)
            The remainder of the string sent with the command.
            See README.md for task string syntax.
          prepend: A bool to prepend (true) or append (false).
        """
        parts = args.split(None,1)
        rospy.loginfo("mission_manager: Adding task with arguments: %s"%parts)
        if len(parts) == 2:
            task = None
            task_type = parts[0]
            task_list = []
            if task_type == 'mission_plan':
                task_list = parseMission(json.loads(parts[1]), self.default_speed)
            elif task_type == 'goto':
                task = parseLatLong(args)
                if task is not None:
                    task['type'] = 'goto'
                    task_list.append(task)
            elif task_type == 'hover':
                task = parseLatLong(args)
                if task is not None:
                    task['type'] = 'hover'
                    task_list.append(task)
            else:
                rospy.logerr("mission_manager: No defined task of type <%s> "
                             "from task string <%s>"%(task_type, args))
                
            if ((task is not None) or (len(task_list) > 0)): 
                if prepend:
                    self.tasks = task_list + self.task
                else:
                    self.tasks += task_list
            else:
                rospy.logerr("mission_manager: The task string <%s> was "
                             "not successfully parsed. No task added!"%
                             args)
        else:
            rospy.logerr("mission_manager: Task string <%s> was not "
                         "split into exactly two parts.  No task added!")
            
        rospy.loginfo('mission_manager: tasks : %s'%str(self.tasks))

    def setOverride(self, task):
        """ TODO: Describe what this does?

        Args:
          task: 
            A dictionary defining the task. 
            TODO: Point to where the task dictionary sematics are described.
        """
        self.override_task = task
        self.pending_command = 'do_override'
        
    
    def waypointReached(self, lat, lon):
      """ TODO: Write Doc"""
      d = self.robot_nav.distanceBearingTo(lat, lon)[0]
      if d is None:
        return False
      return d < self.waypointThreshold


    def generatePathFromVehicle(self, targetLat, targetLon, targetHeading):
        """ Wraps geneatePath() to create path.

        Crates path from current position/heading
        to target position/heading

        TODO: This should not be a method of the object.  Should be a general
              purpose function, probably in project11 module.  
              Not specific to this program.

        TODO: Should be more specific name, such as 
              generateDubinsPathFromVehicle()

        Args:
          targetLon: A float, Latitude - degrees
          targetLon: A float, Longitude - degrees
          targetHeading: A float, degrees, NED
        Returns:
           An array of geographic_msgs/GeoPose objects
           geographic_msgs/GeoPose[]
        """
        
        p = self.robot_nav.positionLatLon()
        h = self.robot_nav.heading()
        #rospy.loginfo('generatePathFromVehicle',p,h)
        return self.generatePath(math.degrees(p[0]), math.degrees(p[1]),
                                 h, targetLat, targetLon, targetHeading)


    def generatePath(self, startLat, startLon, startHeading,
                     targetLat, targetLon, targetHeading):
        """ Create path from start to target.

        Uses move_base's make_plan service, if avaiable, to generate a path
        from start to target. Falls back to generateDubinsPath is the make_plan
        service is not available.

        Args:
          startLat: Float,  Latitude - degrees
          startLon: Float, Longitude - degrees
          startHeading: Float, degrees, NED.
          targetLat: Float, Latitude - degrees
          targetLon: Float, Longitude - degrees
          targetHeading: Float degrees, NED.
        Returns:
           An array of geographic_msgs/GeoPose objects
           geographic_msgs/GeoPose[]
        """

        try:
            rospy.wait_for_service('move_base/make_plan', timeout=0.5)
        except rospy.ROSException as e:
            rospy.logwarn_throttle(10, str(e) + " - move_base/make_plan not available, falling back to Dubin's")
            return self.generateDubinsPath(startLat, startLon, startHeading,
                     targetLat, targetLon, targetHeading)

        make_plan_service = rospy.ServiceProxy('move_base/make_plan', GetPlan)
        
        plan_request = GetPlanRequest()
        plan_request.start = self.robot_nav.geoToPose(startLat, startLon, startHeading)
        plan_request.goal = self.robot_nav.geoToPose(targetLat, targetLon, targetHeading)
        
        plan = make_plan_service(plan_request).plan

        rospy.logdebug(plan)

        return self.robot_nav.poseListToGeoPoseList(plan.poses)




    def generateDubinsPath(self, startLat, startLon, startHeading,
                     targetLat, targetLon, targetHeading):
        """ Create Dubin's curve path from start to target.

        Uses the dubins_curves ROS project services to create a path
        from start to target.

        TODO: This should not be a method of the object.  Should be a general
              purpose function, probably in project11 module.  
              Not specific to this program.

        Args:
          startLat: Float,  Latitude - degrees
          startLon: Float, Longitude - degrees
          startHeading: Float, degrees, NED.
          targetLat: Float, Latitude - degrees
          targetLon: Float, Longitude - degrees
          targetHeading: Float degrees, NED.
        Returns:
           An array of geographic_msgs/GeoPose objects
           geographic_msgs/GeoPose[]
        """
        #rospy.loginfo('generatePath: from:',startLat,startLon,
        #   'to:',targetLat,targetLon)
        service_name = 'dubins_curves_latlong'
        try:
            rospy.wait_for_service(service_name, timeout=0.5)
        except rospy.ROSException as e:
            rospy.logerr("mission_manager: %s"%str(e))
            # Return an empty list 
            return []
            
        dubins_service = rospy.ServiceProxy('dubins_curves_latlong',
                                            DubinsCurvesLatLong)

        # Setup service request
        dubins_req = DubinsCurvesLatLongRequest()
        # See cfg/mission_manager.cfg for more verbose explanations
        # Dynamic reconfig param - typ. 10.0 m 
        dubins_req.radius = self.turnRadius
        # Dynamic reconfig param - typ. 5 m 
        dubins_req.samplingInterval = self.segmentLength

        dubins_req.startGeoPose.position.latitude = startLat
        dubins_req.startGeoPose.position.longitude = startLon

        start_yaw = math.radians(project11.nav.headingToYaw(startHeading))
        start_quat = quaternion_from_euler(0.0,0.0,start_yaw)
        dubins_req.startGeoPose.orientation.x = start_quat[0]
        dubins_req.startGeoPose.orientation.y = start_quat[1]
        dubins_req.startGeoPose.orientation.z = start_quat[2]
        dubins_req.startGeoPose.orientation.w = start_quat[3]
        
        dubins_req.targetGeoPose.position.latitude = targetLat
        dubins_req.targetGeoPose.position.longitude = targetLon
      
        target_yaw = math.radians(project11.nav.headingToYaw(targetHeading))
        q = quaternion_from_euler(0.0,0.0,target_yaw)
        dubins_req.targetGeoPose.orientation.x = q[0]
        dubins_req.targetGeoPose.orientation.y = q[1]
        dubins_req.targetGeoPose.orientation.z = q[2]
        dubins_req.targetGeoPose.orientation.w = q[3]

        
        # Call the service
        rospy.logdebug("mission_manager: Calling Dubins path service with "
                      "request: %s"%str(dubins_req))
        dubins_path = dubins_service(dubins_req)
        
        # Return the service output
        rospy.logdebug("mission_manager: Generated the following Dubins path: "
                      "%s"%str(dubins_path))
        return dubins_path.path


    def iterate(self, current_state):
        """ Affords query by state machine.

        Method called by the SMACH state execution as the interface between
        the state machines and the MissionManagerCore.
        
        TODO: Returning either a string or None is ill-defined and confusing.

        TODO: It appears that returning None is intended to indicate that the 
        smach states should continue.  It would improve clarity if the function 
        returned something more overt, e.g., 'continue'

        TODO: Create more consist return strings. E.g., if they are going to be
        phrased as commands, they should be {exit, pause, cancel}.


        Args:
          current_state: 
            A str current_state Arbitrary string - typically the name of the 
            SMACH state object that called the function.
        
        Returns:
          A str OR None.
          The str communicates to SMACH state classes what to do next
                 which can be {'exit', 'pause', 'cancelled'} 
                                 or None
        """
        if rospy.is_shutdown():
            rospy.loginfo("mission_manager: ROS is shutdown, so telling "
                          "state to 'exit'")
            return 'exit'
        if self.getPilotingMode() != 'autonomous':
            rospy.loginfo("mission_manager: Piloting mode is not "
                          "'autonomous', but instead is <%s>, "
                          "so telling state to 'pause'"%self.piloting_mode)
            return 'pause'
        if self.pending_command is not None:
            rospy.loginfo("mission_manager: There is no pending_command, "
                          "so telling the state 'cancelled'")
            return 'cancelled'
        # Publish the Heartbeat message
        # TODO: Why not publish status if one of the above conditions are met?
        self.publishStatus(current_state)
        # TODO: Why this fixed sleep?  Need to improve and parameterize.
        # TODO: This sleep is likely redundant with sleep calls in the
        #       smach states.
        rospy.sleep(0.1)
        return None          

    def nextTask(self):
        """ Executed by the NextTask class execute method.
        
        TODO: Write more documentation about what this does.
        """

        rospy.loginfo('mission_manager.nextTask: pending_command: %s'%
                      str(self.pending_command))

        # do_override:
        if self.pending_command == 'do_override':
            if ( (self.current_task is not None) and
                 (self.current_task['type'] == 'mission_plan') ):
                self.current_task['current_path'] = None
            self.saved_task = self.current_task
            self.pending_command = None
            return
        
        if self.override_task is not None:
            self.current_task = self.saved_task
            if self.current_task is None and len(self.tasks):
                self.current_task = self.tasks[0]
            self.override_task = None
            if self.pending_command == 'next_task':
                self.pending_command = None
                return

        if self.pending_command == 'restart_mission' and len(self.tasks):
            for t in self.tasks:
                if t['type'] == 'mission_plan':
                    t['current_nav_objective_index'] = None
                    t['current_path'] = None
            self.current_task = self.tasks[0]

        if self.pending_command in ('next_task','prev_task'):
            if len(self.tasks):
                if self.current_task is None:
                    if self.pending_command == 'next_task':
                        self.current_task = self.tasks[0]
                    if self.pending_command == 'prev_task':
                        self.current_task = self.tasks[-1]
                else:
                    try:
                        i = self.tasks.index(self.current_task)
                        rospy.loginfo('nextTask: current task index: %d'%i)
                        if self.pending_command == 'next_task':
                            i += 1
                            if i >= len(self.tasks):
                                self.current_task = None
                            else:
                                self.current_task = self.tasks[i]
                        if self.pending_command == 'prev_task':
                            i -= 1
                            if i < 0:
                                self.current_task = None
                            else:
                                self.current_task = self.tasks[i]
                    except ValueError:
                        rospy.loginfo("nextTask: can't find current task index!")
                        self.current_task = None
                    if self.current_task is None: #end of the list or error figuring out where in the list we were.
                        if self.done_behavior == 'restart':
                            self.current_task = self.tasks[0]
                        elif self.done_behavior == 'hover':
                            self.current_task = {'type':'hover'}
                            position = self.robot_nav.positionLatLon()
                            self.current_task['latitude'] = math.degrees(position[0])
                            self.current_task['longitude'] = math.degrees(position[1])
                if self.current_task is not None and self.current_task['type'] == 'mission_plan':
                    self.current_task['current_nav_objective_index'] = None
                    self.current_task['current_path'] = None

                    
        if (self.current_task is not None and
            self.current_task['type'] == 'mission_plan' and
            (self.pending_command is not None and
             ( self.pending_command.startswith('goto_line') or
               self.pending_command.startswith('start_line')))):
            parts = self.pending_command.strip().split(None,1)
            if len(parts) == 2:
                cmd = parts[0]
                line_no = int(parts[1])
                if line_no >= 0 and line_no < len(self.current_task['nav_objectives']):
                    self.current_task['current_nav_objective_index'] = line_no
                    self.current_task['current_path'] = None
                    if cmd == 'goto_line':
                        self.current_task['do_transit'] = False
                    if cmd == 'start_line':
                        self.current_task['do_transit'] = True
                
        
        self.pending_command = None

    
    def getCurrentTask(self):
        """ Returns either the current_task or the override_task attribute

        TODO: Surprising behavior that it doesn't always access 
        the current_task attribute.  Get rid of the access method. 
        Python doesn't have private attributes.

        Returns:
          A Task dict
        """
        if self.override_task is not None:
            return self.override_task
        return self.current_task

    
    def publishStatus(self, state):
        """ ROS publish Heartbeat message with mission, task and state info.
        
        Publish Heatbeat message with mission, task and state information
        stuffed into the key/value pairs of the message.

        TODO: Add a list of allowable state strings and validate that the 
              input argument is one one of the allowed states.
        
        Args:
          state: An arbitrary str that is added to the Heartbeat
                          message as the value associated with key=state.
        """
        hb = Heartbeat()
        hb.header.stamp = rospy.Time.now()

        gvi = GeoVizItem()
        gvi.id = 'mission_manager'
        
        hb.values.append(KeyValue('state',state))
        hb.values.append(KeyValue('tasks_count',str(len(self.tasks))))


        lastPosition = None
        lastHeading = None
        if self.current_task is None:
            p = self.robot_nav.positionLatLon()
            if p is not None:
                lastPosition = {'latitude': math.degrees(p[0]),
                                'longitude': math.degrees(p[1])}
            lastHeading = self.robot_nav.heading()

        for t in self.tasks:
            tstring = t['type']
            if t['type'] == 'mission_plan' and 'label' in t:
                tstring += ' ('+t['label']+')'
            hb.values.append(KeyValue('-task',tstring))

            if t['type'] == 'mission_plan':
                for track_num in range(len(t['nav_objectives'])):
                    nav_o = t['nav_objectives'][track_num]
                    nextHeading = None
                    if len(nav_o['waypoints']) >= 2:
                        wp1 = nav_o['waypoints'][0]
                        wp2 = nav_o['waypoints'][1]
                        nextHeading = project11.nav.distanceBearingDegrees(wp1['latitude'],
                                                                           wp1['longitude'],
                                                                           wp2['latitude'],
                                                                           wp2['longitude'])[1]
                    elif ((len(nav_o['waypoints']) == 1) and
                          (lastPosition is not None)):
                        wp1 = nav_o['waypoints'][0]
                        nextHeading = self.segmentHeading(
                            lastPosition['latitude'],
                            lastPosition['longitude'],
                            wp1['latitude'],
                            wp1['longitude'])
                    if nextHeading is not None and lastHeading is not None:
                        gvpl = GeoVizPointList() # transit line
                        gvpl.color.a = 0.5
                        gvpl.color.r = 0.4
                        gvpl.color.g = 0.4
                        gvpl.color.b = 0.4
                        gvpl.size = 2
                        pre_start = project11.geodesic.direct(
                            math.radians(wp1['longitude']),
                            math.radians(wp1['latitude']),
                            math.radians(nextHeading+180),
                            self.lineup_distance)
                        for p in self.generatePath(lastPosition['latitude'],
                                                   lastPosition['longitude'],
                                                   lastHeading,
                                                   math.degrees(pre_start[1]),
                                                   math.degrees(pre_start[0]),
                                                   nextHeading):
                            gvpl.points.append(p.position)
                        gp = GeoPoint()
                        gp.latitude = wp1['latitude']
                        gp.longitude = wp1['longitude']
                        gvpl.points.append(gp)
                        gvi.lines.append(gvpl)
                    if len(nav_o['waypoints']):
                        gvpl = GeoVizPointList() # track line
                        gvpl.color.a = 0.75
                        gvpl.color.r = 0.65
                        gvpl.color.g = 0.4
                        gvpl.color.b = 0.75
                        gvpl.size = 3
                        for wp in nav_o['waypoints']:
                            gp = GeoPoint()
                            gp.latitude = wp['latitude']
                            gp.longitude = wp['longitude']
                            gvpl.points.append(gp)
                        gvi.lines.append(gvpl)
                        lastPosition = nav_o['waypoints'][-1]
                        if len(nav_o['waypoints']) >= 2:
                            wp1 = nav_o['waypoints'][-2]
                            wp2 = nav_o['waypoints'][-1]
                            lastHeading = project11.nav.distanceBearingDegrees(wp1['latitude'],
                                                              wp1['longitude'],
                                                              wp2['latitude'],
                                                              wp2['longitude'])[1]

        self.display_publisher.publish(gvi)                
                    
        if self.current_task is None:
            hb.values.append(KeyValue('current_task','None'))
        else:
            hb.values.append(KeyValue('current_task_type',
                                      self.current_task['type']))
            if self.current_task['type'] == 'mission_plan':
                hb.values.append(KeyValue('current_task_label',
                                          self.current_task['label']))
                hb.values.append(
                    KeyValue('current_task_nav_objective_count',
                             str(len(self.current_task['nav_objectives']))))
                hb.values.append(
                    KeyValue('current_task_nav_objective_index',
                             str(self.current_task['current_nav_objective_index'])))
        self.status_publisher.publish(hb)
               
class MMState(smach.State):
    """Base state for Mission Manager states.

    TODO: The update rate / sleep period is fixed in the states. 
    Add a base class method and attribute to parameterize the update rate.
    """
    def __init__(self, mm, outcomes):
        smach.State.__init__(self, outcomes=outcomes)
        self.missionManager = mm
        
class Pause(MMState):
    """This state is for all top level piloting_mode other than autonomous

    TODO: Instead of a semi-infinite loop, have this state transition
    back to itself if the mode is still not 'autonomous'

    Stays in this state while piloting_mode is not 'autonomous'
    and ros is not shutdown.
    """
    def __init__(self, mm):
        MMState.__init__(self, mm, outcomes=['resume','exit'])
        
    def execute(self, userdata):
        while self.missionManager.piloting_mode != 'autonomous':
            if rospy.is_shutdown():
                return 'exit'
            self.missionManager.publishStatus('Pause')
            rospy.sleep(0.1)
        return 'resume'

class Idle(MMState):
    """In autonomous mode, but with no pending tasks.

    TODO: The 'exit' outcome is specified, but there is no associated 
    transition.  Should remove the outcome or define the transition.
    
    """
    def __init__(self, mm):
        MMState.__init__(self, mm, outcomes=['exit','do-task','pause'])
        
    def execute(self, userdata):
        """
        Loop repeates infinitely
          if ( (interate() returns None) and
               (missionManager.tasks queue is empty) )

        TODO: Why is userdata included, but never used?

        TODO: Replace semi-infinite loop with state transition back to same
              state.
        """
        while True:
            ret = self.missionManager.iterate('Idle')
            if ret == 'cancelled':
                return 'do-task'
            # TODO: Increase readabilty with
            # elif ret in ['pause', 'cancelled']:
            if ret is not None:
                return ret
            if len(self.missionManager.tasks) != 0:
                return 'do-task'

class NextTask(MMState):
    """ TODO: Write doc.
    """
    def __init__(self, mm):
        MMState.__init__(self, mm, outcomes=['idle','mission_plan',
                                             'goto','hover'])
        
    def execute(self, userdata):
        self.missionManager.nextTask()
        if self.missionManager.override_task is not None:
            return self.missionManager.override_task['type']
        if self.missionManager.current_task is not None:
            return self.missionManager.current_task['type']
        return 'idle'

class Hover(MMState):
    """SMACH state object to interface with hover ROS action.

    Interfaces with the hover action - see hover repository for action spec.
    
    """
    def __init__(self, mm):

        MMState.__init__(self, mm, outcomes=['cancelled', 'exit', 'pause', 'follow_path'])
        self.hover_client = actionlib.SimpleActionClient('hover_action', hover.msg.hoverAction)
        
    def execute(self, userdata):
        task = self.missionManager.getCurrentTask()
        if task is not None:
            if not self.missionManager.waypointReached(task['latitude'],task['longitude']):

                headingToPoint = self.missionManager.robot_nav.distanceBearingTo(task['latitude'],task['longitude'])[1]
                path = self.missionManager.generatePathFromVehicle(task['latitude'],task['longitude'],headingToPoint)
                if (len(path) > 1):
                    task['path'] = path
                    task['path_type'] = 'transit'
                    task['default_speed'] = self.missionManager.default_speed
                    return 'follow_path'
            goal = hover.msg.hoverGoal()
            goal.target.latitude = task['latitude']
            goal.target.longitude = task['longitude']
            rospy.loginfo("mission_manager.Hover: Sending goal to hover "
                          "action server: %s"%str(goal))
            to = 2.0
            if (not self.hover_client.wait_for_server(rospy.Duration(to))):
                rospy.logerr("mission_manager.Hover: Connection to hover "
                             "action server timed out after %.2f s"%to)
                return 'cancelled'
            self.hover_client.send_goal(goal,
                                        active_cb = self.callbackActive,
                                        feedback_cb = self.callbackFeedback,
                                        done_cb = self.callbackDone)
        # TODO: Would be more clear...
        # ret = None
        # while (ret is None):
        while True:
            # TODO: Update the status rospy.loginfo
            #  through the action feedback interface.
            ret = self.missionManager.iterate('Hover')
            if ret is not None:
                self.hover_client.cancel_goal()
                return ret
    def callbackActive(self):
        rospy.loginfo("mission_manager: hover action is active.")
    def callbackFeedback(self, feedback):
        rospy.loginfo_throttle(2.0, "mission_manager: hover action feedback: \n"
                      "\t range: %.2f, bearing: %.2f, speed: %.2f"%
                      (feedback.range, feedback.bearing, feedback.speed))
    def callbackDone(self, state, result):
        rospy.loginfo("mission_manager: hover action done: \n"
                      "\t state: %s ,result: %s"%(str(state),str(result)))

class LineEnded(MMState):
    """SMACH state object.

    TODO: This state doesn't appear to have much purpose in the 
          GOTO use case, were we transition here from GOTO and then 
          straight to NEXTTASK
    """
    def __init__(self,mm):
        MMState.__init__(self, mm, outcomes=['mission_plan','next_item'])

    def execute(self, userdata):
        """TODO: Write brief doc.

        For the GOTO use-case, this just sets pending_command and 
        transitions to NEXTTASK.

        TODO: Describe the MISSIONPLAN use-case
        """
        task = self.missionManager.getCurrentTask()
        if task is not None and task['type'] == 'mission_plan':
            if task['transit_path'] is not None:
                task['transit_path'] = None
                self.missionManager.endofline_publisher.publish("transit")
            else:
                task['current_path'] = None
                task['current_nav_objective_index'] += 1
                self.missionManager.endofline_publisher.publish("track")
            return 'mission_plan'
        self.missionManager.pending_command = 'next_task'
        return 'next_item'

        
class MissionPlan(MMState):
    """TODO: Write doc.
    """
    def __init__(self, mm):
        MMState.__init__(self, mm, outcomes=['follow_path',
                                             'survey_area','done'])
        
    def execute(self, userdata):
        task = self.missionManager.current_task
        if task is not None:
            if (task['current_nav_objective_index'] is None):
                task['current_nav_objective_index'] = 0
            if (task['current_nav_objective_index'] >=
                len(task['nav_objectives'])):
                task['current_nav_objective_index'] = None
                self.missionManager.pending_command = 'next_task'
                return 'done'
            if (task['nav_objectives'][task['current_nav_objective_index']]['type'] == 'SurveyArea'):
                return 'survey_area'
            if ((not 'current_path' in task) or
                (task['current_path'] is None)):
                self.generatePaths(task)
            return 'follow_path'
        return 'done'

    def generatePaths(self, task):
        path = []
        rospy.loginfo(task['nav_objectives'])
        for p in task['nav_objectives'][task['current_nav_objective_index']]['waypoints']:
            #path.append((p['position']['latitude'],p['position']['longitude']))
            gp = GeoPose()
            gp.position.latitude = p['latitude']
            gp.position.longitude = p['longitude']
            path.append(gp)
        task['current_path'] = path
        # decide if we transit or start line
        task['transit_path'] = None
        if len(task['current_path']) >1:
            start_point = task['current_path'][0]
            next_point = task['current_path'][1]
            if task['do_transit'] and self.missionManager.robot_nav.distanceBearingTo(start_point.position.latitude,start_point.position.longitude)[0] > self.missionManager.waypointThreshold and self.missionManager.planner == 'path_follower':
                #transit
                segment_heading =  project11.nav.distanceBearingDegrees(start_point.position.latitude,start_point.position.longitude,next_point.position.latitude,next_point.position.longitude)[1]
                pre_start = project11.geodesic.direct(math.radians(start_point.position.longitude), math.radians(start_point.position.latitude), math.radians(segment_heading+180), self.missionManager.lineup_distance)
                #print ('heading',segment_heading, 'start:', start_point.position, 'pre start:',  math.degrees(pre_start[1]), math.degrees(pre_start[0]))
                transit_path = self.missionManager.generatePathFromVehicle(math.degrees(pre_start[1]), math.degrees(pre_start[0]), segment_heading)
                transit_path.append(start_point)
                task['transit_path'] = transit_path
            task['do_transit'] = True
        
class Goto(MMState):
    """
    SMACH state object
    

    """
    def __init__(self, mm):
        MMState.__init__(self, mm, outcomes=['done','follow_path','cancelled'])
        
    def execute(self, userdata):
        """
        If we are close to waypoint - returns 'done'
        Otherwise, plans the Dubin's path and returns 'follow_path'

        TODO: The MissionManagerCore object is too intertwined with this 
        function.  The methods being used should be 
        independent of MissionManagerCore.

        TODO: The path is stuffed back into the MissionManagerCore.task
        attribute.  Would it be cleaner to use userdata to pass the path
        to the follower?
        """

        #return self.execute_move_base(userdata)

        task = self.missionManager.getCurrentTask()
        if task is not None:
            if self.missionManager.waypointReached(task['latitude'], task['longitude']):
                self.missionManager.pending_command = 'next_task'
                return 'done'
            headingToPoint = self.missionManager.robot_nav.distanceBearingTo(task['latitude'],task['longitude'])[1]
            rospy.loginfo("mission_manager.GOTO: Generating Dubin's path.")
            path = self.missionManager.generatePathFromVehicle(
                task['latitude'],task['longitude'],headingToPoint)
            if (len(path) < 1):
                return 'done'
            rospy.loginfo("mission_manager.GOTO: Generated Dubin's path: %s"% str(path))
            task['path'] = path
            task['path_type'] = 'transit'
            task['default_speed'] = self.missionManager.default_speed
            return 'follow_path'
        else:
            rospy.logerr("mission_manager.GOTO: "
                          "MissionManagerCore.getCurrentTask() returns None "
                          "- so GOTO state has undefined return.")

    def execute_move_base(self, userdata):
        task = self.missionManager.getCurrentTask()
        if task is not None:
            if not self.missionManager.waypointReached(task['latitude'],task['longitude']):
                headingToPoint = self.missionManager.headingToPoint(
                    task['latitude'],task['longitude'])
                goal = move_base_msgs.msg.MoveBaseGoal()
                goal.target_pose = self.missionManager.geoToPose(task['latitude'],task['longitude'],headingToPoint, self.missionManager.map_frame)
                distance = self.missionManager.distanceTo(task['latitude'],task['longitude'])
                rospy.loginfo("distance: "+str(distance))
                rospy.loginfo("speed: "+str(self.missionManager.default_speed))
                duration = rospy.Duration.from_sec(distance/self.missionManager.default_speed)
                rospy.loginfo("duration: "+str(duration.to_sec()))
                goal.target_pose.header.stamp = rospy.Time.now()+duration
                rospy.loginfo("Goto: "+str(goal))
                move_base_client = actionlib.SimpleActionClient('move_base', move_base_msgs.msg.MoveBaseAction)
                self.move_base_done = False
                to = 2.0
                if (not move_base_client.wait_for_server(rospy.Duration(to))):
                    rospy.logerr("mission_manager.Goto: Connection to move_base "
                             "action server timed out after %.2f s"%to)
                    return 'cancelled'
                move_base_client.send_goal(goal,
                                           done_cb = self.callbackDone,
                                           active_cb = self.callbackActive,
                                           feedback_cb= self.callbackFeedback)
                while True:
                    ret = self.missionManager.iterate('Goto')
                    if ret is not None:
                        move_base_client.cancel_goal()
                        return ret
                    if self.move_base_done:
                        self.missionManager.pending_command = 'next_task'
                        return 'done'
        return 'done'

                    

 

    def callbackActive(self):
        rospy.loginfo("mission_manager: goto move_base action is active.")

    def callbackFeedback(self, feedback):
        rospy.loginfo_throttle(2.0, "mission_manager: goto move_base action feedback: \n"+str(feedback))

    def callbackDone(self, state, result):
        self.move_base_done = True
        rospy.loginfo("mission_manager: goto move_base action done: \n"
                      "\t"+str(state)+str(result))


class FollowPath(MMState):
    """SMACH state object.

    Uses either path_follower action OR path_planner action, 
    depending on the string attribute MissionManagerCore.planner.


    """
    def __init__(self, mm):
        """
        Initiates path_follower and path_planner action clients.
        """
        MMState.__init__(self, mm, outcomes=['done','cancelled',
                                             'exit','pause', 'hover'])
        self.path_follower_client = actionlib.SimpleActionClient(
            'path_follower_action', 
            path_follower.msg.path_followerAction)
        self.path_planner_client = actionlib.SimpleActionClient(
            'path_planner_action', 
            path_planner.msg.path_plannerAction)
        self.task_complete = False
        
    def execute(self, userdata):
        task = self.missionManager.getCurrentTask()
        if task is not None:
            if self.missionManager.planner == 'path_follower':
                goal = path_follower.msg.path_followerGoal()
            elif self.missionManager.planner == 'path_planner':   
                goal = path_planner.msg.path_plannerGoal()
            goal.path.header.stamp = rospy.Time.now()
            if task['type'] in ('goto', 'hover'):
                path = task['path']
            if task['type'] == 'mission_plan':
                if task['transit_path'] is not None:
                    path = task['transit_path']
                else:
                    path = task['current_path']
            for s in path:
                #print s
                gpose = GeoPoseStamped()
                gpose.pose = s
                goal.path.poses.append(gpose)
            goal.speed = task['default_speed']
            self.task_complete = False
            # Sends goal to either path_follower or
            # path_planner action client.

            """
            The planner attribute is a string which specifies which
            path follwer action client to use.
            Here we assign the generic 'follower_client' object 
            based on that string.

            TODO: path_follower and path_planner should use the same
                  action interface.  Currently they each use their own 
                  interface description.  This interface should be generalized
                  for future "follower" actions server/clients as well.
            """
            # Default is path_follower
            follower_client = self.path_follower_client
            if self.missionManager.planner == 'path_follower':
                self.path_planner_client.cancel_goal()
                follower_client = self.path_follower_client
            elif self.missionManager.planner == 'path_planner':
                self.path_follower_client.cancel_goal()
                follower_client = self.path_planner_client
            else:
                rospy.logerr("mission_manager: Undefined behavior for "
                             "MissionManagerCore.planner == <%s>.  "
                             "Cancelling FollowPath!"
                             %self.missionManager.planner)
                return 'cancelled'
        
            # Wait for client server, with timeout
            to = 2.0
            if (not follower_client.wait_for_server(
                    rospy.Duration(to))):
                rospy.logerr("mission_manager.FollowPath: "
                             "Connection to path_follower "
                             "action server timed out after %.2f s"%to)
                return 'cancelled'
            # Send goal path that was planned by Goto State
            follower_client.send_goal(
                goal,
                self.callbackFollowerDone,
                self.callbackFollowerActive,
                self.callbackFollowerFeedback)
        
        while True:
            ret = self.missionManager.iterate('FollowPath')
            # TODO: Get and report feedback from action client.
            if ret is not None:
                if ret == 'cancelled':
                    if self.missionManager.planner == 'path_follower':
                        self.path_follower_client.cancel_goal()
                    elif self.missionManager.planner == 'path_planner':
                        self.path_planner_client.cancel_goal()
                return ret
            if self.task_complete:
                if task['type'] == 'hover':
                    return 'hover'
                return 'done'
            if ((task['type'] == 'hover') and
                (self.missionManager.waypointReached(task['latitude'],
                                                     task['longitude']))):
              if self.missionManager.planner == 'path_follower':
                self.path_follower_client.cancel_goal()
              elif self.missionManager.planner == 'path_planner':
                self.path_planner_client.cancel_goal()
              return 'hover'

    def callbackFollowerActive(self):
        """Callback for follower action client interface.
        """
        rospy.loginfo("mission_manager: follower action client is active.")    
    
    def callbackFollowerFeedback(self, feedback):
        """Callback for follower action client interface.
        """
        rospy.loginfo_throttle(2.0, "mission_manager.FollowPath: "
                               "follower action feedback: "
                               "%s"%str(feedback))
        # TODO: Not sure hte point of this?
        task = self.missionManager.getCurrentTask()
        if task is not None:
            if 'path_type' in task and task['path_type'] == 'transit':
                pass
        
    
    def callbackFollowerDone(self, status, result):
        """Callback for follower action client interface
        """
        rospy.loginfo("mission_manager.FollowPath: follower action done: \n"
                      "\t status: %s ,result: %s"%(str(status),str(result)))
        # Tell MissionManagerCore that the task is complete
        self.task_complete = True


class SurveyArea(MMState):
    def __init__(self, mm):
        MMState.__init__(self, mm, outcomes=['done','cancelled',
                                             'exit','pause'])
        self.survey_area_client = actionlib.SimpleActionClient(
            'survey_area_action', manda_coverage.msg.manda_coverageAction)
        self.task_complete = False

    def execute(self, userdata):
        task = self.missionManager.current_task
        if task is not None:
            goal = manda_coverage.msg.manda_coverageGoal()
            for wp in task['nav_objectives'][task['current_nav_objective_index']]['children']:
                rospy.loginfo(wp)
                if wp["type"] == 'Waypoint':
                    gp = GeoPoint()
                    gp.latitude = wp['latitude']
                    gp.longitude = wp['longitude']
                    goal.area.append(gp)

            goal.speed = task['default_speed']
            self.task_complete = False
            to = 2.0
            if (not self.survey_area_client.wait_for_server(
                    rospy.Duration(to))):
                rospy.logerr("mission_manager.SurveyArea: Connection to "
                             "survey_area "
                             "action server timed out after %.2f s"%to)
                return 'cancelled'
            self.survey_area_client.send_goal(
                goal,
                self.survey_area_done_callback,
                self.survey_area_active_callback,
                self.survey_area_feedback_callback)

        while True:
            ret = self.missionManager.iterate('SurveyArea')
            if ret is not None:
                if ret == 'cancelled':
                    self.survey_area_client.cancel_goal()
                return ret
            if self.task_complete:
                return 'done'

    def survey_area_done_callback(self, status, result):
        self.task_complete = True
    
    def survey_area_active_callback(self):
        pass
    
    def survey_area_feedback_callback(self, msg):
        pass
    
    
def main():
    """Main.

    Instatiates MissionManagerCore and SMACH state machine(s)
    """
    rospy.init_node('MissionManager')
    
    missionManager = MissionManagerCore()

    sm_top = smach.StateMachine(outcomes=['exit'])
    
    with sm_top:
        smach.StateMachine.add('PAUSE', Pause(missionManager),
                               transitions={'resume':'AUTONOMOUS',
                                            'exit':'exit'})
        
        sm_auto = smach.StateMachine(outcomes=['pause','exit'])
        
        with sm_auto:
            smach.StateMachine.add('IDLE',
                                   Idle(missionManager),
                                   transitions={'do-task':'NEXTTASK',
                                                'pause':'pause'})
            smach.StateMachine.add('NEXTTASK',
                                   NextTask(missionManager),
                                   transitions={'idle':'IDLE',
                                                'mission_plan':'MISSIONPLAN',
                                                'hover':'HOVER',
                                                'goto':'GOTO'})
            smach.StateMachine.add('HOVER', Hover(missionManager),
                                   transitions={'pause':'pause',
                                                'cancelled':'NEXTTASK',
                                                'follow_path':'FOLLOWPATH'})
            smach.StateMachine.add('MISSIONPLAN', MissionPlan(missionManager),
                                   transitions={'done':'NEXTTASK',
                                                'follow_path':'FOLLOWPATH',
                                                'survey_area':'SURVEYAREA'})
            smach.StateMachine.add('GOTO',Goto(missionManager),
                                   transitions={'done':'NEXTTASK',
                                                'cancelled':'NEXTTASK',
                                                'follow_path':'FOLLOWPATH'})
            smach.StateMachine.add('FOLLOWPATH', FollowPath(missionManager),
                                   transitions={'pause':'pause',
                                                'cancelled':'NEXTTASK',
                                                'done':'LINEENDED',
                                                'hover':'HOVER'})
            smach.StateMachine.add('LINEENDED', LineEnded(missionManager),
                                   transitions={'mission_plan': 'MISSIONPLAN',
                                                'next_item':'NEXTTASK'})
            smach.StateMachine.add('SURVEYAREA', SurveyArea(missionManager),
                                   transitions={'pause':'pause',
                                                'cancelled':'NEXTTASK',
                                                'done':'NEXTTASK'})

        smach.StateMachine.add('AUTONOMOUS', sm_auto,
                               transitions={'pause':'PAUSE',
                                            'exit':'exit'})
    
    sis = smach_ros.IntrospectionServer('mission_manager', sm_top,
                                        '/mission_manager')
    sis.start()                                                                            

    sm_top.execute()
    rospy.spin()
    
if __name__ == '__main__':
    main()
