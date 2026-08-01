================================================================================
  V-SHAPE IPM MOTOR AI OPTIMIZER v5.2 (FIX LAN 3 REMOTE) - TECHNICAL DOCUMENTATION
================================================================================

--------------------------------------------------------------------------------
1. TỔNG QUAN HỆ THỐNG & CẢI TIẾN TRONG PHÂN BẢN FIX LẦN 3
--------------------------------------------------------------------------------

Hệ thống AI Optimizer v5.2 (Fix Lần 3 Remote) là công cụ tối ưu hóa đa mục tiêu
cấp công nghiệp dành cho Động cơ Nam châm Vĩnh cửu Chìm hình chữ V (V-Shape IPM Motor).
Hệ thống sử dụng thuật toán NSGA-II 4D Pareto Hypervolume kết hợp với mô hình 
Surrogate AI 4 đầu ra (Gaussian Process / KNN) và giao tiếp trực tiếp với Ansys
Maxwell 3D thông qua PyAEDT hoặc win32com ActiveX.

Các cải tiến & sửa lỗi quan trọng trong phân bản Fix Lần 3:
--------------------------------------------------------------------------------
1. RAM Guard & Auto Session Restart:
   - Tự động theo dõi dung lượng RAM trống bằng psutil.
   - Khi RAM trống giảm xuống dưới 50% tổng RAM hệ thống, tự động đóng toàn bộ
     dự án, thoát và khởi động lại session Ansys Electronics Desktop COM 
     (Quit + taskkill ansysedt.exe + reconnect).
   - Loại bỏ hoàn toàn sự cố rò rỉ bộ nhớ (OOM) và lỗi "The RPC server is unavailable".

2. Fix Lỗi COM Thread Affinity (_analyze_with_timeout):
   - Thực thi trực tiếp oDesign.Analyze("Setup1") trên Main Thread khởi tạo COM.
   - Triệt tiêu hoàn toàn các lỗi AttributeError ('SetActiveDesign.Analyze') và 
     'CoInitialize has not been called' xuất hiện khi chạy headless trên máy từ xa/VM.

3. Thử lại dọn dẹp file tạm 5 lần (_cleanup_temp_project_files):
   - Tự động lặp lại 5 lần (khoảng dừng 0.5s) khi xóa các file tạm .aedt, 
     .aedt.lock, .aedt.auto và thư mục .aedtresults.
   - Xử lý dứt điểm tình trạng khóa file tạm thời từ các tiến trình Ansys chạy ngầm.

4. Cloudpaging Player & Safe Headless Mode:
   - Tự động gọi SetIconic(True) khi chạy ẩn (--non-graphical).
   - Đảm bảo tiến trình chạy mượt mà trên các môi trường ảo hóa bản quyền Cloudpaging
     mà không cướp tiêu điểm màn hình hay gây treo ứng dụng.

5. Mô hình Surrogate ML 4 Đầu Ra & Confidence Fallback (MLSurrogate):
   - Duy trì 4 mô hình Gaussian Process (kernel RBF) / KNN độc lập cho 4 mục tiêu.
   - Khi độ không đảm bảo (uncertainty / standard deviation) ở BẤT KỲ mục tiêu nào
     vượt quá 2.5%, hệ thống tự động kích hoạt mô phỏng FEA Ansys thực tế.

6. Auto Warm-Start từ Lịch sử:
   - Tự động nạp cá thể xuất sắc nhất từ file lịch sử simulation_history.csv 
     vào quần thể ban đầu (population[0]).

7. Phân tích Nhạy cảm Đa mục tiêu (Per-Objective Sensitivity Mutation):
   - Tính toán hệ số tương quan thứ hạng Spearman cho từng thông số đối với 
     từng mục tiêu trong số 4 mục tiêu để điều hướng đột biến thông minh.

8. Tìm kiếm Cục bộ Elite (Multi-Objective Elite Local Search):
   - Tạo các biến động bước nhỏ xung quanh các cá thể Pareto Rank-0 tốt nhất, 
     kiểm tra lại tất cả các ràng buộc hình học và tiêu chí áp đảo Pareto.

9. Cấu trúc Thư mục Kết quả Theo Thời Gian:
   - Kết quả lưu tự động vào outputs/run_YYYYMMDD_HHMMSS/ đồng thời tạo/cập nhật 
     liên kết outputs/latest/ trỏ đến lần chạy mới nhất.


--------------------------------------------------------------------------------
2. 4 MỤC TIÊU TỐI ƯU HÓA (4-OBJECTIVE PARETO ENGINE)
--------------------------------------------------------------------------------

Engine NSGA-II tối ưu hóa đồng thời 4 mục tiêu cốt lõi:

1. Efficiency (Hiệu suất η):
   - Công thức: Pout / Pin * 100 [%]
   - Hướng tối ưu: TỐI ĐA HÓA (MAXIMIZE)

2. Power Density (Mật độ Công suất):
   - Công thức: Pout / Wtotal [kW/kg]
   - Hướng tối ưu: TỐI ĐA HÓA (MAXIMIZE)

3. Material Cost (Chi phí Vật liệu):
   - Công thức: Sum(Vi * costPerVolume_i) [$] (Thép stator + Nam châm NdFeB)
   - Hướng tối ưu: TỐI THIỂU HÓA (MINIMIZE)

4. Torque Ripple (Độ nhấp nhô Momen):
   - Công thức: pk2pk(Torque) / mean(Torque) * 100 [%]
   - Hướng tối ưu: TỐI THIỂU HÓA (MINIMIZE)

Điểm Số Thứ Cấp (Secondary Composite Score - dùng cho báo cáo & xếp hạng phụ):
   Score = (w_eff * Eff) - (w_ripple * Ripple) + (w_pwr * Pwr) - (w_cost * (Cost / 150))


--------------------------------------------------------------------------------
3. 19 BIẾN THIẾT KẾ & 10 BIẾN PHỤ THUỘC
--------------------------------------------------------------------------------

19 Biến Thiết kế Tự do (được nạp từ Ai_Optimization_Bounds.xlsx):
+--------------+-------------------+-------------+-------------+------------+--------+
| Tên Biến     | Mô tả Chi tiết    | Giới hạn Dưới| Giới hạn Trên| Bước Nhảy  | Đơn vị |
+--------------+-------------------+-------------+-------------+------------+--------+
| Dr_in        | Đường kính trong  | 50.0        | 90.0        | 5.0        | mm     |
| Air_gap      | Khe hở không khí  | 0.5         | 1.5         | 0.1        | mm     |
| Lamda        | Hệ số chiều dài   | 0.8         | 1.0         | 0.1        | -      |
| Bridge       | Độ dày cầu từ     | 1.0         | 3.0         | 0.1        | mm     |
| Hs0          | Chiều cao miệng rãnh 1.0        | 2.0         | 0.1        | mm     |
| Hs1          | Chiều cao vai rãnh | 1.0        | 2.0         | 0.1        | mm     |
| Hs2          | Chiều cao thân rãnh| 16.0       | 30.0        | 1.0        | mm     |
| Bs0          | Chiều rộng miệng  | 1.5         | 4.0         | 0.5        | mm     |
| Bs1          | Chiều rộng vai    | 3.0         | 10.0        | 0.5        | mm     |
| Bs2          | Chiều rộng đáy    | 5.0         | 14.0        | 1.0        | mm     |
| O1           | Khoảng cách hốc V1| 0.0         | 13.0        | 1.0        | mm     |
| O2           | Khoảng cách hốc V2| 2.0         | 7.0         | 0.5        | mm     |
| B1           | Chiều rộng hốc NC | 3.2         | 5.0         | 0.5        | mm     |
| rib          | Độ rộng gân trung tâm 2.0       | 15.0        | 1.0        | mm     |
| hrib         | Chiều cao gân     | 2.0         | 6.0         | 0.5        | mm     |
| Mt           | Độ dày nam châm   | 4.0         | 6.0         | 0.2        | mm     |
| Mw           | Bề rộng nam châm  | 10.0        | 30.0        | 2.0        | mm     |
| magDmin      | Đáy hốc nam châm  | 0.0         | 10.0        | 1.0        | mm     |
| Thet_deg     | Góc V nam châm    | 0.0         | 90.0        | 1.0        | deg    |
+--------------+-------------------+-------------+-------------+------------+--------+

10 Biến Phụ thuộc (Tính toán động tự động cho từng thiết kế):
- D_ag      : Đường kính khe hở không khí = L_STK / Lamda
- Ds_in     : Đường kính trong Stator = D_ag + Air_gap
- Dr_out    : Đường kính ngoài Rotor = Ds_in - 2 * Air_gap
- Speed_rpm : Tốc độ đồng bộ = 120 * f0 / Pole = 1000 RPM
- A_slot    : Diện tích rãnh Stator = 213.17 mm2
- D1        : Đường kính trong Rotor sau cầu từ = Ds_in - 2*Air_gap - 2*Bridge
- Acond     : Diện tích dây dẫn = Imax / J = 200 / 5.5 = 36.36 mm2
- N         : Số vòng dây mỗi rãnh = ceil(0.7 * A_slot / Acond)
- t0        : Thời điểm ban đầu = 0.0 ms
- Thet      : Góc Momen tính bằng Radian = Thet_deg * pi / 180


--------------------------------------------------------------------------------
4. 6 RÀNG BUỘC HÌNH HỌC (GEOMETRIC CONSTRAINTS)
--------------------------------------------------------------------------------

Mọi cá thể đều phải thỏa mãn nghiêm ngặt 6 ràng buộc hình học sau:
1. SlotHeight           : Hs0 + Hs1 + Hs2 < ((Ds_out - Ds_in) / 2) - 12.25 mm
2. SlotWidthProgression : Bs0 <= Bs1 và Bs1 <= Bs2
3. BridgeThickness      : B1 <= Mt - 0.3 mm
4. RotorFitsStator      : Dr_out > Dr_in
5. MagnetDuctFit        : Mw > 2 * B1
6. RibHeightLimit       : hrib <= min(O2, 4.5, Bridge * 2)

Hệ thống tích hợp thuật toán repair_individual tự động sửa chữa cá thể vi phạm
trước khi đưa vào mô phỏng.


--------------------------------------------------------------------------------
5. HƯỚNG DẪN CÀI ĐẶT VÀ SỬ DỤNG
--------------------------------------------------------------------------------

Cài đặt Môi trường:
-------------------
1. Mở PowerShell hoặc Command Prompt tại thư mục dự án.
2. Kích hoạt Virtual Environment:
   - PowerShell: .\.venv\Scripts\Activate.ps1
   - CMD       : .\.venv\Scripts\activate.bat
3. Cài đặt các thư viện cần thiết:
   pip install -r requirements.txt

Cú pháp Lệnh Chạy (CLI Command):
--------------------------------
python "motor_optimizer_ver5.2(fix lan3)_remote.py" [tùy chọn]

Các Lệnh Thực Tế Khuyên Dùng:
-----------------------------

1. Chạy Mô phỏng FEA Ansys Maxwell Trực tiếp (Khuyên dùng trong Production):
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --mode ansys --algorithm nsga2 --pop-size 8 --generations 10 --plot-all

2. Chạy Bộ Kiểm thử Hệ thống (Built-in Unit Tests - 15 test cases):
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --test

3. Chạy Mô phỏng Offline bằng AI Surrogate (Siêu nhanh - vài giây):
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --mode offline --pop-size 12 --generations 30 --plot-all

4. Tiếp tục Chạy từ Checkpoint bị ngắt quãng (--resume):
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --resume --mode ansys --generations 20

5. Chạy qua Cầu nối MATLAB ActiveX:
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --mode matlab --algorithm nsga2 --pop-size 10 --generations 8


--------------------------------------------------------------------------------
6. BẢNG THAM SỐ COMMAND-LINE (CLI OPTIONS)
--------------------------------------------------------------------------------

+-------------------+---------------+---------------------------------------------------------------------------+
| Tham số CLI       | Mặc định      | Mô tả & Hướng dẫn sử dụng                                                 |
+-------------------+---------------+---------------------------------------------------------------------------+
| --mode            | offline       | Chế độ mô phỏng: ansys (PyAEDT/ActiveX), matlab (MATLAB bridge), offline. |
| --algorithm       | nsga2         | Thuật toán: nsga2 (Đa mục tiêu NSGA-II) hoặc ga (Di truyền đơn mục tiêu). |
| --pop-size        | 8             | Kích thước quần thể (Số lượng mẫu đánh giá mỗi thế hệ).                   |
| --generations     | 10            | Số lượng thế hệ tối đa.                                                   |
| --crossover       | 0.7           | Xác suất lai ghép (Crossover probability).                                |
| --mutation        | 0.2           | Tỷ lệ đột biến gen (Mutation rate per gene).                              |
| --w-eff           | 1.0           | Trọng số Hiệu suất trong điểm tổng hợp (Efficiency weight).               |
| --w-ripple        | 1.0           | Trọng số Độ nhấp nhô Momen trong điểm tổng hợp (Torque Ripple weight).    |
| --w-pwr           | 0.5           | Trọng số Mật độ công suất (Power Density weight).                         |
| --w-cost          | 0.05          | Trọng số Chi phí vật liệu (Cost penalty weight).                          |
| --non-graphical   | True          | Chạy Ansys Maxwell không giao diện (Headless mode) tiết kiệm RAM/CPU.     |
| --show-gui        | False         | Hiển thị giao diện Ansys Maxwell (tương đương --no-non-graphical).        |
| --resume          | False         | Khôi phục trạng thái từ checkpoint optimizer_state.pkl.                   |
| --plot-all        | False         | Tự động tạo tất cả 4 biểu đồ phân tích & hội tụ khi hoàn thành.           |
| --plot-pareto     | False         | Tự động tạo biểu đồ 2D Pareto Front.                                      |
| --test            | False         | Chạy bộ 15 Unit Tests kiểm thử toàn bộ hệ thống rồi thoát.                |
| --no-ml           | False         | Tắt mô hình AI Surrogate, buộc mô phỏng FEA 100%.                         |
| --no-local-search | False         | Tắt tính năng tìm kiếm cục bộ Elite.                                      |
| --no-report       | False         | Bỏ qua việc tạo báo cáo Markdown (optimization_report.md).               |
| --keep-temp       | False         | Giữ lại các file dự án .aedt tạm thời sau khi hoàn tất.                   |
| --seed            | None          | Hạt giống ngẫu nhiên (Random seed) để tái lập kết quả.                    |
+-------------------+---------------+---------------------------------------------------------------------------+


--------------------------------------------------------------------------------
7. CẤU TRÚC THƯ MỤC KẾT QUẢ ĐẦU RA (OUTPUTS)
--------------------------------------------------------------------------------

Tất cả kết quả của mỗi lần chạy được lưu tự động vào thư mục:
   outputs/run_YYYYMMDD_HHMMSS/ (được liên kết bởi outputs/latest/)

Các tệp kết quả bao gồm:
- best_optimized_design_v5.2.csv : Thông số 19 biến và chỉ số của thiết kế tối ưu nhất.
- simulation_history.csv         : Lịch sử đầy đủ của toàn bộ cá thể đã đánh giá.
- log_history.csv                : Nhật ký chi tiết kết quả mô phỏng và vi phạm ràng buộc.
- optimizer.log                  : Nhật ký thực thi hệ thống chi tiết theo thời gian.
- optimization_report.md         : Báo cáo tổng quan dạng Markdown tự động tạo.
- pareto_front.png               : Đồ thị Pareto 2D (Hiệu suất vs Độ nhấp nhô Momen).
- pareto_3d.png                  : Đồ thị Pareto 3D (Hiệu suất vs Độ nhấp nhô Momen vs Chi phí).
- parallel_coordinates.png       : Đồ thị Tọa độ Song song 4 mục tiêu.
- convergence_history.png        : Đồ thị Hội tụ 4D Hypervolume & Best Score qua các thế hệ.

================================================================================
