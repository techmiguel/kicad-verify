# Design intent

Read by the independent reviewer. Facts the schematic cannot show; keep it short.
Taken from the board's README (smartRele v3).

- Purpose of the board: single-channel Wi-Fi relay module based on the ESP-12F (ESP8266)
- Supply input (voltage range, source, max current): 110-120 VAC 50/60 Hz mains through an isolated HLK-PM01 (5 V), T500mA time-lag fuse and 07D221K varistor
- Loads and their currents (motors, relays, LEDs, radios): mains load up to 10 A resistive through a 16 A Omron G5RL relay; ESP-12F Wi-Fi; status LEDs
- External connectors and what plugs into them: J1 3-pole 5.08 mm terminal block (N, L_OUT, L_IN); J3 2x3 programming header powered at 5 V
- Environment (temperature, humidity, mains, battery, enclosure): indoor, 110-120 VAC mains, printed enclosure
- Target fab/assembly house and process: JLCPCB, 2 layers 1.6 mm, PCB assembly top side (SMD and THT)
- Decisions that look wrong but are intentional: 2 x 2.5 mm duplicated power path sized for 10 A with ~12 C rise (IPC-2221); 8 mm coil-to-contact separation; antenna at the board edge with no copper underneath
