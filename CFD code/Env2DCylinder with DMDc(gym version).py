import gymnasium as gym
from gymnasium import spaces
from pydmd import DMDc
from pydmd.plotter import plot_eigs
from fenics import *
from mshr import *
import numpy as np
import matplotlib.pyplot as plt
import meshio
from math import sin, cos, pi
from matplotlib.animation import FuncAnimation
import gymnasium as gym
from gymnasium import spaces


class Env2DCylinder(gym.Env):
    metadata = {"render_modes": ["human"], "render_fps": 30}

    def epsilon(self, u):
        return sym(nabla_grad(u))

    def sigma(self, u, p):
        return 2 * self.mu * self.epsilon(u) - p * Identity(len(u))

    def __init__(self):

        super().__init__()

        # Files for storing the data
        self.xdmffile_u = XDMFFile('ns_cylinder/velocity.xdmf')
        self.xdmffile_p = XDMFFile('ns_cylinder/pressure.xdmf')

        self.timeseries_u = TimeSeries('ns_cylinder/velocity_series')
        self.timeseries_p = TimeSeries('ns_cylinder/pressure_series')

        self.mesh_for_cylinder = File('ns_cylinder/cylinder.xml.gz')

        self.action_space = spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32)
        self.observation_space = spaces.Box(low=-100, high=100, shape=(126,), dtype=np.float64)

        self.T = 20
        self.num_steps = 40000
        self.dt = self.T / self.num_steps
        self.D = 0.1
        self.Re = 100
        self.U_m = 1.5
        self.mu = 2 * (self.U_m) * (self.D) / (3 * (self.Re))
        self.rho = 1
        self.Q0 = 0
        self.snap_num = 30
        self.actuator_num = 2
        self.snapshot_fre = 500  # The snapshots are updated every 50 time steps.

        self.Qmax = 100

        # self.action_space = gym.spaces.Box(low=-self.Qmax,high=self.Qmax,shape=(1,),dtype=np.float32)

        self.t = 0
        self.n = 0
        self.Cd_max = 0
        self.Cl_max = 0
        self.x = []
        self.y = []
        self.z = []

        D = self.D

        self.drag_coefficient, self.lift_coefficient = 0, 0
        self.avg_drag, self.avg_lift = 0, 0

        self.probes = []

        self.locations = []
        self.l1 = 0.55 * D
        self.l2 = 0.8 * D

        self.mem_episode = 2500
        self.mem_state = []
        self.avg_drag_len = 25
        self.drag_list = [0] * self.avg_drag_len
        self.avg_lift_len = 25
        self.lift_list = [0] * self.avg_lift_len

        self.drag_mem = [0] * self.avg_drag_len
        self.lift_mem = [0] * self.avg_lift_len

        #         for theta in np.linspace(0,2*pi,13):
        #             self.locations.append((2*D+self.l1*sin(theta),2*D+self.l1*cos(theta)))

        #         for theta in np.linspace(0,2*pi,13):
        #             self.locations.append((2*D+self.l2*sin(theta),2*D+self.l2*cos(theta)))

        for x in np.linspace(2.5 * D, 5 * D, 9):
            for y in np.linspace(1.2 * D, 2.8 * D, 7):
                self.locations.append((x, y))

        self.probes_num = len(self.locations)

        self.p_column = np.zeros((self.probes_num, 1))
        self.u_column = np.zeros((self.actuator_num, 1))

        self.X_dmd = np.zeros((self.probes_num, self.snap_num))
        self.Y_dmd = np.zeros((self.actuator_num, self.snap_num - 1))

        self.dmdc = DMDc(svd_rank=-1)
        self.modal_amplitudes = []
        self.reward = 0

        # Read the mesh
        # self.mesh = Mesh('navier_stokes_cylinder/cylinder.xml.gz')

        # Create mesh
        channel = Rectangle(Point(0, 0), Point(22 * D, 4.1 * D))
        cylinder = Circle(Point(2 * D, 2 * D), 0.5 * D)
        domain = channel - cylinder
        self.mesh = generate_mesh(domain, 220)

        # Refine the mesh near the cylinder
        cell_markers = MeshFunction("bool", self.mesh, self.mesh.topology().dim())
        cell_markers.set_all(False)
        origin = Point(2 * D, 2 * D)
        for cell in cells(self.mesh):
            p = cell.midpoint()
            if p.distance(origin) < 0.12:
                cell_markers[cell] = True
        self.mesh = refine(self.mesh, cell_markers, redistribute=True)

        self.dx_CFL = CellDiameter(self.mesh)
        #         self.dx_CFL = sqrt((2.0**self.mesh.geometry().dim())**(-1)*assemble(self.dx_CFL**2*dx))
        #         self.dx_CFL = sqrt(assemble(self.dx_CFL**2)/assemble(1,self.mesh))
        #         self.dx_CFL = sqrt(assemble(self.dx_CFL)/self.mesh.num_cells())
        #         self.CFL_num = (self.U_m*self.dt)/self.dx_CFL

        #         self.plot_mesh()
        #         print("The current mesh has",self.mesh.num_cells(),"cells")
        #         print("The CFL number of the current setting is:",self.CFL_num)

        # Store the mesh
        #         self.mesh_for_cylinder << self.mesh

        self.V = VectorFunctionSpace(self.mesh, 'P', 2)
        self.Q = FunctionSpace(self.mesh, 'P', 1)
        self.W = FunctionSpace(self.mesh, 'CG', 1)
        self.Y = FunctionSpace(self.mesh, 'CG', 1)

        self.V_test = VectorFunctionSpace(self.mesh, 'P', 2)
        self.Q_test = FunctionSpace(self.mesh, 'P', 1)

        # Define boundaries
        inflow = 'near(x[0],0)'
        outflow = 'near(x[0],2.2)'
        walls = 'near(x[1],0)||near(x[1],0.41)'
        cylinder = 'on_boundary && x[0]>0.1 && x[0]<0.3 && x[1] >0.1 && x[1] <0.3'
        self.jet_top = 'on_boundary && x[0]>(0.2-0.05*sin(5*pi/180)) && x[0]<(0.2+0.05*sin(5*pi/180)) && x[1]>0.2 && x[1]<0.3'
        self.jet_bottom = 'on_boundary && x[0]>(0.2-0.05*sin(5*pi/180)) && x[0]<(0.2+0.05*sin(5*pi/180)) && x[1]<0.2 && x[1]>0.1'

        # Inflow profile
        inflow_profile = ('4.0*(U_m)*x[1]*(0.41-x[1])/pow(0.41,2)', '0')
        inflow_f = Expression(inflow_profile, U_m=Constant(1.5), degree=2)

        # Jet profile. Jet1 is at the top of the cylinder, and Jet2 is at the bottom of the cylinder.
        self.jet1_f = Expression(
            ('cos(atan2(x[1],x[0]))*cos(pi*(atan2(x[1],x[0])-theta0_1)/width)*Qjet*pi/(2*width*pow(radius,2))', \
             'sin(atan2(x[1],x[0]))*cos(pi*(atan2(x[1],x[0])-theta0_1)/width)*Qjet*pi/(2*width*pow(radius,2))'), \
            Qjet=0, width=Constant(10), radius=Constant(10), theta0_1=Constant(0.5 * pi), degree=2)

        self.jet2_f = Expression(
            ('cos(atan2(x[1],x[0]))*cos(pi*(atan2(x[1],x[0])-theta0_2)/width)*(-Qjet)*pi/(2*width*pow(radius,2))', \
             'sin(atan2(x[1],x[0]))*cos(pi*(atan2(x[1],x[0])-theta0_2)/width)*(-Qjet)*pi/(2*width*pow(radius,2))'), \
            Qjet=0, width=Constant(10), radius=Constant(10), theta0_2=Constant(1.5 * pi), degree=2)

        # boundary conditions.
        self.bcu_inflow = DirichletBC(self.V, inflow_f, inflow)
        self.bcu_walls = DirichletBC(self.V, Constant((0, 0)), walls)
        self.bcu_cylinder = DirichletBC(self.V, Constant((0, 0)), cylinder)
        self.bcp_outflow = DirichletBC(self.Q, Constant(0), outflow)
        self.bcp = [self.bcp_outflow]
        self.bcu_jet_top = DirichletBC(self.V, self.jet1_f, self.jet_top)
        self.bcu_jet_bottom = DirichletBC(self.V, self.jet2_f, self.jet_bottom)
        self.bcu = [self.bcu_inflow, self.bcu_walls, self.bcu_cylinder, self.bcu_jet_top, self.bcu_jet_bottom]

        # Trial and Test functions
        self.u = TrialFunction(self.V)
        self.v = TestFunction(self.V)
        self.p = TrialFunction(self.Q)
        self.q = TestFunction(self.Q)

        # Functions for solutions at previous and current time steps
        self.u_n = Function(self.V)
        self.u_ = Function(self.V)
        self.p_n = Function(self.Q)
        self.p_ = Function(self.Q)
        self.w_ = Function(self.W)

        self.ux_trial = Function(self.Y)
        self.uy_test = Function(self.Y)

        self.u_mem = Function(self.V)
        self.p_mem = Function(self.Q)

        # Expressions used in variational forms
        self.U = 0.5 * (self.u_n + self.u)
        n = -FacetNormal(self.mesh)
        f = Constant((0, 0))
        k = Constant(self.dt)
        mu = Constant(self.mu)

        # Variational problem for step 1
        F1 = self.rho * dot((self.u - self.u_n) / k, self.v) * dx \
             + self.rho * dot(dot(self.u_n, nabla_grad(self.u_n)), self.v) * dx \
             + inner(self.sigma(self.U, self.p_n), self.epsilon(self.v)) * dx \
             + dot(self.p_n * n, self.v) * ds - dot(mu * nabla_grad(self.U) * n, self.v) * ds \
             - dot(f, self.v) * dx
        self.a1 = lhs(F1)
        self.L1 = rhs(F1)

        # Variational problem for step 2
        self.a2 = dot(nabla_grad(self.p), nabla_grad(self.q)) * dx
        self.L2 = dot(nabla_grad(self.p_n), nabla_grad(self.q)) * dx - (1 / k) * div(self.u_) * (self.q) * dx

        # Variational problem for step 3
        self.a3 = dot(self.u, self.v) * dx
        self.L3 = dot(self.u_, self.v) * dx - k * dot(nabla_grad(self.p_ - self.p_n), self.v) * dx

        # Assemble matrices
        self.A1 = assemble(self.a1)
        self.A2 = assemble(self.a2)
        self.A3 = assemble(self.a3)

        # Apply bcs to matrices
        [bc.apply(self.A1) for bc in self.bcu]
        [bc.apply(self.A2) for bc in self.bcp]

        # self.memorize_state()

    def update_jetBCs(self, new_Qjet):
        self.jet1_f.Qjet = new_Qjet
        self.jet2_f.Qjet = new_Qjet
        self.bcu_jet_top = DirichletBC(self.V, self.jet1_f, self.jet_top)
        self.bcu_jet_bottom = DirichletBC(self.V, self.jet2_f, self.jet_bottom)
        self.bcu = [self.bcu_inflow, self.bcu_walls, self.bcu_cylinder, self.bcu_jet_top, self.bcu_jet_bottom]

        # Apply bcs to matrices
        [bc.apply(self.A1) for bc in self.bcu]
        [bc.apply(self.A2) for bc in self.bcp]

    def compute_drag_lift_coefficients(self, u, p):
        # Define normal vector along the cylinder surface
        rho = self.rho
        D = self.D
        n = FacetNormal(self.mesh)
        #     stress_tensor=sigma(u,p_n)
        stress_tensor = self.sigma(u, p)

        boundary_parts = MeshFunction("size_t", self.mesh, self.mesh.topology().dim() - 1)
        boundary_parts.set_all(0)

        class CylinderBoundary(SubDomain):
            def inside(self, x, on_boundary):
                tol = 1E-14
                return on_boundary and x[0] > 0.1 and x[0] < 0.3 and x[1] > 0.1 and x[1] < 0.3

        Gamma_1 = CylinderBoundary()
        Gamma_1.mark(boundary_parts, 1)

        ds = Measure('ds', domain=self.mesh, subdomain_data=boundary_parts, subdomain_id=1)

        force = dot(stress_tensor, n)
        drag_force = assemble(force[0] * ds)
        lift_force = assemble(force[1] * ds)
        # Compute drag and lift coefficients
        drag_coefficient = abs(2 * drag_force / (rho * 1.0 * D))
        lift_coefficient = abs(2 * lift_force / (rho * 1.0 * D))

        return drag_coefficient, lift_coefficient

    def get_reward(self):
        Cd, Cl = self.compute_drag_lift_coefficients(self, u, p)
        return -Cd - 0.2 * Cl  # reward function ?

    def evolve(self, a):
        #         assert self.action_space.contains(a),"Invalid Action Provided!"

        t = self.t
        n = self.n
        x = self.x
        y = self.y
        z = self.z
        dt = self.dt

        # update the BCs with a Qjet number
        self.update_jetBCs(a)

        # 1 Tentative velocity step
        self.b1 = assemble(self.L1)
        [bc.apply(self.b1) for bc in self.bcu]
        solve(self.A1, self.u_.vector(), self.b1, 'bicgstab', 'hypre_amg')

        # 2 pressure correction step
        self.b2 = assemble(self.L2)
        [bc.apply(self.b2) for bc in self.bcp]
        solve(self.A2, self.p_.vector(), self.b2, 'bicgstab', 'hypre_amg')

        # 3 Velocity correction step
        self.b3 = assemble(self.L3)
        solve(self.A3, self.u_.vector(), self.b3, 'cg', 'sor')

        self.drag_coefficient, self.lift_coefficient = self.compute_drag_lift_coefficients(self.u_, self.p_)
        self.drag_list.pop(0)
        self.drag_list.append(self.drag_coefficient)
        self.avg_drag = np.mean(self.drag_list)
        self.lift_list.pop(0)
        self.lift_list.append(self.lift_coefficient)
        self.avg_lift = np.mean(self.lift_list)

        if self.t == 5000 * self.dt:
            self.xdmffile_u.write(self.u_, self.t)
            self.xdmffile_p.write(self.p_, self.t)
            self.timeseries_u.store(self.u_.vector(), self.t)
            self.timeseries_p.store(self.p_.vector(), self.t)
            print("Current time step:", self.n, "Data saved.")

        if self.drag_coefficient > self.Cd_max and self.n > 49:
            self.Cd_max = self.drag_coefficient
        if self.lift_coefficient > self.Cl_max and self.n > 49:
            self.Cl_max = self.lift_coefficient

        if self.n % 50 == 0 and n > 1:
            i = n / 50
            self.x.append(self.n)
            self.y.append(self.drag_coefficient)
            self.z.append(self.lift_coefficient)

        if self.n == self.num_steps - 5:
            plt.plot(x, y)
            plt.show()

            # print("Time step:",n,"Cd:",drag_coefficient,"Cl",lift_coefficient)

        if self.n % 200 == 0:
            print("Current time step:", self.n)
            self.plot_p_field()
        #             self.w_ = self.compute_vorticity(self.u_)
        #             self.plot_w_field()

        if self.n % 30 == 0 and self.n <= 900:
            self.update_DMDc_data(a)

        if self.n % self.snapshot_fre == 0 and self.n > 900:  # Update the matrices X_dmd and Y_dmd and perform the DMDc
            self.update_DMDc_data(a)
            self.dmdc.fit(self.X_dmd, self.Y_dmd)
            self.modal_amplitudes = self.dmdc.amplitudes
            self.reward = 0.198 - 0.1 * np.abs(self.modal_amplitudes[0])
            for i in range(self.snap_num - 2):
                self.reward = self.reward - 0.0005 * np.abs(self.modal_amplitudes[i + 1])

        self.u_n.assign(self.u_)
        self.p_n.assign(self.p_)

        observation = np.array(self.probes_vp())
        terminated = False
        truncated = False
        info = {
            "Environment": self.probes_num
        }

        self.t += dt
        self.n += 1

        #         assert self.observation_space.contains(observation),"Invalid Observation Provided!"
        # done=self.n>self.num_steps
        # return s_,r,done
        return observation, self.reward, terminated, truncated, info

    #         return probe_results,self.get_reward(self.avg_drag,self.avg_lift),False

    # return probe_results,self.get_reward(self.drag_coefficient,self.lift_coefficient),done

    def step(self, action):
        for i in range(self.snapshot_fre - 1):
            self.evolve(action)
        observation, reward, terminated, truncated, info = self.evolve(action)
        return observation, reward, terminated, truncated, info

    def reset(self, seed=None, options=None):
        self.memorize_state()
        self.start_with_memory()
        observation = self.probes_vp()
        observation = np.array(observation)
        info = {
            "Environment": self.probes_num
        }
        return observation, info

    def render(self):
        print("Render")

    def close(self):
        print("Closed")

    def evolve_n(self, n, a=0):
        for i in range(n):
            self.evolve(a)

    def memorize_state(self):
        for n in range(self.mem_episode):
            self.evolve(0)
        #             if n%200==0:
        #                 fig=plt.figure(figsize=(220,41),dpi=100)
        #                 plot(self.p_)
        #                 for p in self.locations:
        #                     plt.scatter(p[0],p[1],color='red',s=300)
        #                 plt.show()

        self.u_mem = self.u_
        self.p_mem = self.p_
        self.drag_mem = self.drag_list
        self.lift_mem = self.lift_list

        self.mem_state = self.probes_vp()

    def start_with_memory(self):
        self.t = self.dt * self.mem_episode
        self.n = self.mem_episode
        self.Cd_max = 0
        self.Cl_max = 0
        self.x = []
        self.y = []
        self.z = []

        self.drag_coefficient, self.lift_coefficient = 0, 0
        self.avg_drag, self.avg_lift = 0, 0

        self.u_ = self.u_mem
        self.p_ = self.p_mem
        self.drag_list = self.drag_mem
        self.lift_list = self.lift_mem
        self.probes = self.mem_state

    def plot_mesh(self):
        fig = plt.figure(figsize=(160, 60), dpi=100)
        plot(self.mesh)
        plt.show()

    def plot_p_field(self, show_observation_points=0):

        plt.clf()
        self.p_array = self.p_.compute_vertex_values(self.mesh)
        self.p_array = self.p_array.reshape((self.mesh.num_vertices(),))
        plt.figure(figsize=(11, 2.05))
        plt.tripcolor(self.mesh.coordinates()[:, 0], self.mesh.coordinates()[:, 1], self.mesh.cells(), self.p_array,
                      shading="gouraud", cmap='coolwarm')
        plt.colorbar()

        if show_observation_points == 1:
            x_coords = np.array(self.locations)[:, 0]
            y_coords = np.array(self.locations)[:, 1]
            plt.scatter(x_coords, y_coords, color='black', s=5)

        plt.xlabel('x')
        plt.ylabel('y')
        plt.title('P_field')

        plt.show()

    def plot_w_field(self, show_observation_points=0):

        plt.clf()
        self.w_array = self.w_.compute_vertex_values(self.mesh)
        self.w_array = self.w_array.reshape((self.mesh.num_vertices(),))
        plt.figure(figsize=(11, 2.05))
        plt.tripcolor(self.mesh.coordinates()[:, 0], self.mesh.coordinates()[:, 1], self.mesh.cells(), self.w_array,
                      shading="gouraud", cmap='coolwarm')
        plt.colorbar()

        if show_observation_points == 1:
            x_coords = np.array(self.locations)[:, 0]
            y_coords = np.array(self.locations)[:, 1]
            plt.scatter(x_coords, y_coords, color='black', s=5)

        #             x1_coords = np.array(self.jet_locations)[:, 0]
        #             y1_coords = np.array(self.jet_locations)[:, 1]
        #             plt.scatter(x1_coords, y1_coords, color='navy', s=5)

        plt.xlabel('x')
        plt.ylabel('y')
        plt.title('Vorticity_field')

        #         plt.savefig("Airfoil_Re2500/"+str(self.n/self.num_steps).zfill(6)+"Re2500"+".png")
        if self.n % 200 == 0:
            plt.show()

    def get_reward(self, drag, lift):
        return -drag  # -0.2*lift##+3.18)?*

        #     def reset(self):
        #         self.__init__()
        #         for i in range(1000):
        #             self.evolve(0)

        return self.probes_vp()

    def probes_vp(self):
        self.probes = []
        for p in self.locations:
            self.probes.append(self.u_((p[0], p[1]))[0])
            self.probes.append(self.u_((p[0], p[1]))[1])
            # self.probes.append(self.p_((p[0],p[1])))

        # self.probes=evals
        # self.nprobes=len(locations)
        return self.probes

    def compute_vorticity(self, u):
        mesh = self.mesh

        class VorticityExpression(UserExpression):
            def __init__(self, ux_value, uy_value, degree=1, mesh=mesh):
                self.ux_value = ux_value
                self.uy_value = uy_value
                self.YY = FunctionSpace(mesh, 'P', 1)
                self.ux_trial = Function(self.YY)
                self.ux_trial.vector().set_local(self.ux_value)
                self.uy_test = Function(self.YY)
                self.uy_test.vector().set_local(self.uy_value)
                super().__init__(degree=degree)

            def eval(self, value, x):
                value[0] = self.ux_trial.dx(1)(x) - self.uy_test.dx(0)(x)

            def value_shape(self):
                return ()

        ux, uy = u.split(deepcopy=True)
        VORTICITY = Function(self.W)
        VORTICITY = project(uy.dx(0) - ux.dx(1), self.W)

        return VORTICITY

    def p_column_vector_for_dmd(self):

        for i in range(self.probes_num):
            point = self.locations[i]
            self.p_column[i] = self.p_((point[0], point[1]))

        return self.p_column

    def u_column_vector_for_dmd(self, Q1, Q2):

        self.u_column[0] = Q1
        self.u_column[1] = Q2

        return self.u_column

    def update_matrix(self, matrix, new_column):

        matrix[:, :-1] = matrix[:, 1:]
        matrix[:, -1] = new_column[:, 0]

        return matrix

    def update_DMDc_data(self, a):
        self.p_column_vector_for_dmd()
        self.u_column_vector_for_dmd(a, -a)
        self.update_matrix(self.X_dmd, self.p_column)
        self.update_matrix(self.Y_dmd, self.u_column)

    def pid(self, kp, ki, kd):
        sums = 0
        s = 0
        s_ = 0
        ds = 0
        for i in range(self.num_steps):
            a = kp * s_ + ki * sums + kd * ds
            state, _, _ = self.evolve(a)
            s = state[33]
            sums += s
            ds = (s - s_) / self.dt
            s_ = s

    def pidctl(self):
        self.pid(100, 0, 0)