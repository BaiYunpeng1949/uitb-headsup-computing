import mujoco as mj
from mujoco.glfw import glfw
import numpy as np
from numpy.linalg import inv
import os

xml_path = 'hopper.xml'
simend = 20

step_no = 0

FSM_AIR1 = 0
FSM_STANCE1 = 1
FSM_STANCE2 = 2
FSM_AIR2 = 3

fsm = FSM_AIR1

# For callback functions
button_left = False
button_middle = False
button_right = False
lastx = 0
lasty = 0

def controller(model, data):
    """
    This function implements a controller that
    mimics the forces of a fixed joint before release
    """
    global fsm
    global step_no

    body_no = 3
    z_foot = data.xpos[body_no, 2]
    vz_torso = data.qvel[1]

    # Lands on the ground
    if fsm == FSM_AIR1 and z_foot < 0.05:
        fsm = FSM_STANCE1

    # Moving upward
    if fsm == FSM_STANCE1 and vz_torso > 0.0:
        fsm = FSM_STANCE2

    # Take off
    if fsm == FSM_STANCE2 and z_foot > 0.05:
        fsm = FSM_AIR2

    # Moving downward
    if fsm == FSM_AIR2 and vz_torso < 0.0:
        fsm = FSM_AIR1
        step_no += 1

    if fsm == FSM_AIR1:
        set_position_servo(2, 100)
        set_velocity_servo(3, 10)

    if fsm == FSM_STANCE1:
        set_position_servo(2, 1000)
        set_velocity_servo(3, 0)

    if fsm == FSM_STANCE2:
        set_position_servo(2, 1000)
        set_velocity_servo(3, 0)
        data.ctrl[0] = -0.2

    if fsm == FSM_AIR2:
        set_position_servo(2, 100)
        set_velocity_servo(3, 10)
        data.ctrl[0] = 0.0

def init_controller(model,data):
    # pservo-hip
    set_position_servo(0, 100)

    # vservo-hip
    set_velocity_servo(1, 10)

    # pservo-knee
    set_position_servo(2, 1000)

    # vservo-knee
    set_velocity_servo(3, 0)

def set_position_servo(actuator_no, kp):
    model.actuator_gainprm[actuator_no, 0] = kp
    model.actuator_biasprm[actuator_no, 1] = -kp

def set_velocity_servo(actuator_no, kv):
    model.actuator_gainprm[actuator_no, 0] = kv
    model.actuator_biasprm[actuator_no, 2] = -kv

def keyboard(window, key, scancode, act, mods):
    if act == glfw.PRESS and key == glfw.KEY_BACKSPACE:
        mj.mj_resetData(model, data)
        mj.mj_forward(model, data)

def mouse_button(window, button, act, mods):
    # update button state
    button_left = (glfw.get_mouse_button(
        window, glfw.MOUSE_BUTTON_LEFT) == glfw.PRESS)
    button_middle = (glfw.get_mouse_button(
        window, glfw.MOUSE_BUTTON_MIDDLE) == glfw.PRESS)
    button_right = (glfw.get_mouse_button(
        window, glfw.MOUSE_BUTTON_RIGHT) == glfw.PRESS)

    # update mouse position
    glfw.get_cursor_pos(window)

def mouse_move(window, xpos, ypos):
    # compute mouse displacement, save
    global lastx
    global lasty
    dx = xpos - lastx
    dy = ypos - lasty
    lastx = xpos
    lasty = ypos

    # no buttons down: nothing to do
    if (not button_left) and (not button_middle) and (not button_right):
        return

    # get current window size
    width, height = glfw.get_window_size(window)

    # get shift key state
    PRESS_LEFT_SHIFT = glfw.get_key(
        window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS
    PRESS_RIGHT_SHIFT = glfw.get_key(
        window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS
    mod_shift = (PRESS_LEFT_SHIFT or PRESS_RIGHT_SHIFT)

    # determine action based on mouse button
    if button_right:
        if mod_shift:
            action = mj.mjtMouse.mjMOUSE_MOVE_H
        else:
            action = mj.mjtMouse.mjMOUSE_MOVE_V
    elif button_left:
        if mod_shift:
            action = mj.mjtMouse.mjMOUSE_ROTATE_H
        else:
            action = mj.mjtMouse.mjMOUSE_ROTATE_V
    else:
        action = mj.mjtMouse.mjMOUSE_ZOOM

    mj.mjv_moveCamera(model, action, dx/height,
                      dy/height, scene, cam)

def scroll(window, xoffset, yoffset):
    action = mj.mjtMouse.mjMOUSE_ZOOM
    mj.mjv_moveCamera(model, action, 0.0, -0.05 *
                      yoffset, scene, cam)

#get the full path
dirname = os.path.dirname(__file__)
abspath = os.path.join(dirname + "/" + xml_path)
xml_path = abspath

# MuJoCo data structures
model = mj.MjModel.from_xml_path(xml_path)  # MuJoCo model
data = mj.MjData(model)                # MuJoCo data
cam = mj.MjvCamera()                        # Abstract camera
opt = mj.MjvOption()                        # visualization options

# Init GLFW, create window, make OpenGL context current, request v-sync
glfw.init()
window = glfw.create_window(1200, 900, "Demo", None, None)
glfw.make_context_current(window)
glfw.swap_interval(1)

# initialize visualization data structures
mj.mjv_defaultCamera(cam)
mj.mjv_defaultOption(opt)
scene = mj.MjvScene(model, maxgeom=10000)
context = mj.MjrContext(model, mj.mjtFontScale.mjFONTSCALE_150.value)

# install GLFW mouse and keyboard callbacks
glfw.set_key_callback(window, keyboard)
glfw.set_cursor_pos_callback(window, mouse_move)
glfw.set_mouse_button_callback(window, mouse_button)
glfw.set_scroll_callback(window, scroll)

cam.azimuth = 89.608063
cam.elevation = -11.588379
cam.distance = 5.0
cam.lookat = np.array([0.0, 0.0, 1.5])

init_controller(model,data)

#set the controller
mj.set_mjcb_control(controller)

while not glfw.window_should_close(window):
    simstart = data.time

    while (data.time - simstart < 1.0/60.0):
        mj.mj_step(model, data)

    if (data.time>=simend):
        break;

    # get framebuffer viewport
    viewport_width, viewport_height = glfw.get_framebuffer_size(
        window)
    viewport = mj.MjrRect(0, 0, viewport_width, viewport_height)

    # Update scene and render
    cam.lookat[0] = data.qpos[0] #camera will follow qpos
    mj.mjv_updateScene(model, data, opt, None, cam,
                       mj.mjtCatBit.mjCAT_ALL.value, scene)
    mj.mjr_render(viewport, scene, context)

    # swap OpenGL buffers (blocking call due to v-sync)
    glfw.swap_buffers(window)

    # process pending GUI events, call GLFW callbacks
    glfw.poll_events()

glfw.terminate()
