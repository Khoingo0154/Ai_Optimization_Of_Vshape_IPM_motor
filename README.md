# V-Shape IPM Motor AI Optimizer v5.2 (Fix Lần 3 Remote)

## 📌 Tổng quan Hệ thống

**V-Shape IPM Motor AI Optimizer v5.2 (Fix Lần 3 Remote)** là hệ thống tối ưu hóa đa mục tiêu cấp công nghiệp cho Động cơ Nam châm Vĩnh cửu Chìm hình chữ V (V-Shape Interior Permanent Magnet Motor).

Hệ thống tích hợp bộ công cụ **NSGA-II 4D Pareto Hypervolume**, mô hình **Surrogate AI 4 đầu ra** (Gaussian Process với RBF kernel / KNN-IDW) và cơ chế giao tiếp trực tiếp với **Ansys Maxwell 3D** thông qua PyAEDT (Method A) hoặc win32com ActiveX (Method B).

---

## 🚀 Các Điểm cải tiến & Sửa lỗi quan trọng trong Phân bản Fix Lần 3

1. **RAM Guard & Tự động Restart AEDT Session**:
   - Tự động giám sát dung lượng RAM hệ thống sử dụng `psutil`.
   - Khi dung lượng RAM trống giảm xuống dưới **50%** tổng RAM hệ thống, hệ thống tự động đóng toàn bộ dự án, giải phóng bộ nhớ và khởi động lại session Ansys Electronics Desktop COM (`Quit` + `taskkill` `ansysedt.exe` + reconnect).
   - Loại bỏ hoàn toàn hiện tượng rò rỉ bộ nhớ (OOM) và lỗi ngắt kết nối RPC Server (`The RPC server is unavailable`).

2. **Khắc phục Lỗi COM Thread Affinity (`_analyze_with_timeout`)**:
   - Thực thi trực tiếp hàm `oDesign.Analyze("Setup1")` trên Main Thread giữ kết lộ COM ban đầu.
   - Khắc phục triệt để các ngoại lệ `AttributeError` (`SetActiveDesign.Analyze`) và `CoInitialize has not been called` khi chạy ở chế độ ẩn (headless) trên môi trường Remote hoặc Virtual Machine.

3. **Cơ chế Thử lại Dọn dẹp File tạm 5 lần (`_cleanup_temp_project_files`)**:
   - Tự động thử lại đến 5 lần với khoảng dừng 0.5s để giải phóng các file khóa Windows (`.aedt.lock`, `.aedt.auto`, `.aedtresults`).
   - Xử lý dứt điểm xung đột khóa tệp từ các tiến trình Ansys chạy ngầm.

4. **Tương thích Cloudpaging Player & Safe Headless Mode**:
   - Tự động thiết lập `SetIconic(True)` khi chạy ẩn (`--non-graphical`).
   - Đảm bảo tiến trình chạy mượt mà trên môi trường ảo hóa bản quyền Cloudpaging Player mà không làm chiếm tiêu điểm màn hình hay cướp chuột của người dùng.

5. **Mô hình AI Surrogate 4 Đầu Ra & Confidence Fallback (`MLSurrogate`)**:
   - Duy trì 4 mô hình Gaussian Process / KNN độc lập cho 4 mục tiêu tối ưu.
   - Khi độ không đảm bảo (uncertainty / standard deviation) ở **bất kỳ** mục tiêu nào vượt quá **2.5%**, hệ thống tự động kích hoạt mô phỏng FEA Ansys thực tế.

6. **Tự động Warm-Start từ Lịch sử Mô phỏng**:
   - Nạp cá thể xuất sắc nhất từ file lịch sử `simulation_history.csv` trước đó vào quần thể ban đầu (`population[0]`).

7. **Phân tích Nhạy cảm Đa mục tiêu (Per-Objective Sensitivity Mutation)**:
   - Tính toán hệ số tương quan thứ hạng Spearman riêng biệt cho từng thông số đối với từng mục tiêu trong số 4 mục tiêu để điều hướng đột biến thông minh.

8. **Tìm kiếm Cục bộ Elite Multi-Objective (`perform_elite_local_search`)**:
   - Tạo các biến động bước nhỏ xung quanh các cá thể Pareto Rank-0 tốt nhất, kiểm tra lại tất cả các ràng buộc hình học và tiêu chí áp đảo Pareto.

9. **Cấu trúc Thư mục Kết quả Đầu ra theo Timestamp**:
   - Lưu tự động kết quả vào `outputs/run_YYYYMMDD_HHMMSS/` đồng thời tự động cập nhật liên kết `outputs/latest/` trỏ tới lần chạy gần nhất.

---

## 🎯 4 Mục tiêu Tối ưu hóa (4-Objective Pareto Engine)

Engine NSGA-II tối ưu hóa đồng thời 4 mục tiêu cốt lõi:

| Mục tiêu | Công thức / Định nghĩa | Đơn vị | Hướng Tối ưu |
|---|---|---|---|
| **Efficiency ($\eta$)** | $P_{\text{out}} / P_{\text{in}} \times 100$ | $\%$ | **TỐI ĐA HÓA (MAXIMIZE)** |
| **Power Density** | $P_{\text{out}} / W_{\text{total}}$ | $\text{kW/kg}$ | **TỐI ĐA HÓA (MAXIMIZE)** |
| **Material Cost** | $\sum (V_i \times \text{costPerVolume}_i)$ | $\$$ | **TỐI THIỂU HÓA (MINIMIZE)** |
| **Torque Ripple** | $\frac{\text{pk2pk}(T)}{\text{mean}(T)} \times 100$ | $\%$ | **TỐI THIỂU HÓA (MINIMIZE)** |

### Điểm Số Thứ Cấp (Secondary Composite Score):
$$\text{Score} = (w_{\text{eff}} \times \text{Eff}) - (w_{\text{ripple}} \times \text{Ripple}) + (w_{\text{pwr}} \times \text{Pwr}) - \left(w_{\text{cost}} \times \frac{\text{Cost}}{150}\right)$$

---

## 📐 19 Biến Thiết kế & 10 Biến Phụ thuộc

### 19 Biến Thiết kế Tự do (Đọc từ `Ai_Optimization_Bounds.xlsx`):

| Biến | Mô tả Chi tiết | Giới hạn Dưới | Giới hạn Trên | Bước Nhảy | Đơn vị |
|---|---|---|---|---|---|
| `Dr_in` | Đường kính trong Rotor | 50.0 | 90.0 | 5.0 | mm |
| `Air_gap` | Khe hở không khí | 0.5 | 1.5 | 0.1 | mm |
| `Lamda` | Hệ số chiều dài lõi thép | 0.8 | 1.0 | 0.1 | - |
| `Bridge` | Độ dày cầu từ rotor | 1.0 | 3.0 | 0.1 | mm |
| `Hs0` | Chiều cao miệng rãnh stator | 1.0 | 2.0 | 0.1 | mm |
| `Hs1` | Chiều cao vai rãnh stator | 1.0 | 2.0 | 0.1 | mm |
| `Hs2` | Chiều cao thân rãnh stator | 16.0 | 30.0 | 1.0 | mm |
| `Bs0` | Chiều rộng miệng rãnh stator | 1.5 | 4.0 | 0.5 | mm |
| `Bs1` | Chiều rộng vai rãnh stator | 3.0 | 10.0 | 0.5 | mm |
| `Bs2` | Chiều rộng đáy rãnh stator | 5.0 | 14.0 | 1.0 | mm |
| `O1` | Khoảng cách hốc nam châm V1 | 0.0 | 13.0 | 1.0 | mm |
| `O2` | Khoảng cách hốc nam châm V2 | 2.0 | 7.0 | 0.5 | mm |
| `B1` | Chiều rộng hốc chứa nam châm | 3.2 | 5.0 | 0.5 | mm |
| `rib` | Độ rộng gân trung tâm rotor | 2.0 | 15.0 | 1.0 | mm |
| `hrib` | Chiều cao gân trung tâm | 2.0 | 6.0 | 0.5 | mm |
| `Mt` | Độ dày nam châm vĩnh cửu | 4.0 | 6.0 | 0.2 | mm |
| `Mw` | Bề rộng nam châm vĩnh cửu | 10.0 | 30.0 | 2.0 | mm |
| `magDmin` | Khoảng cách đáy hốc nam châm | 0.0 | 10.0 | 1.0 | mm |
| `Thet_deg` | Góc mở V nam châm vĩnh cửu | 0.0 | 90.0 | 1.0 | deg |

### 10 Biến Phụ thuộc (Tính toán động tự động):
- `D_ag`: Đường kính khe hở không khí = $L_{\text{STK}} / \text{Lamda}$
- `Ds_in`: Đường kính trong Stator = $D_{\text{ag}} + \text{Air\_gap}$
- `Dr_out`: Đường kính ngoài Rotor = $D_{\text{s\_in}} - 2 \times \text{Air\_gap}$
- `Speed_rpm`: Tốc độ đồng bộ = $120 \times f_0 / P = 1000\text{ RPM}$
- `A_slot`: Diện tích rãnh Stator = $213.17\text{ mm}^2$
- `D1`: Đường kính trong Rotor sau cầu từ = $D_{\text{s\_in}} - 2\times\text{Air\_gap} - 2\times\text{Bridge}$
- `Acond`: Diện tích dây dẫn = $I_{\text{max}} / J = 200 / 5.5 = 36.36\text{ mm}^2$
- `N`: Số vòng dây mỗi rãnh = $\lceil 0.7 \times A_{\text{slot}} / A_{\text{cond}} \rceil$
- `t0`: Thời điểm ban đầu = $0.0\text{ ms}$
- `Thet`: Góc Momen tính bằng Radian = $\text{Thet\_deg} \times \pi / 180$

---

## 🔒 6 Ràng buộc Hình học (Geometric Constraints)

Mọi thiết kế động cơ phải thỏa mãn đồng thời 6 điều kiện hình học:
1. **SlotHeight**: $Hs_0 + Hs_1 + Hs_2 < \frac{Ds_{\text{out}} - Ds_{\text{in}}}{2} - 12.25\text{ mm}$
2. **SlotWidthProgression**: $Bs_0 \le Bs_1 \le Bs_2$
3. **BridgeThickness**: $B1 \le Mt - 0.3\text{ mm}$
4. **RotorFitsStator**: $Dr_{\text{out}} > Dr_{\text{in}}$
5. **MagnetDuctFit**: $Mw > 2 \times B1$
6. **RibHeightLimit**: $hrib \le \min(O2, 4.5, 2 \times Bridge)$

---

## 🛠️ Hướng dẫn Cài đặt & Sử dụng

### 1. Kích hoạt Môi trường Virtual Environment
- **PowerShell**:
  ```powershell
  .\.venv\Scripts\Activate.ps1
  ```
- **Command Prompt (CMD)**:
  ```cmd
  .\.venv\Scripts\activate.bat
  ```

### 2. Cài đặt Thư viện Phụ thuộc
```bash
pip install -r requirements.txt
```

---

## 💻 Bảng Lệnh Chạy (CLI Reference)

### Cú pháp Lệnh Chạy:
```bash
python "motor_optimizer_ver5.2(fix lan3)_remote.py" [các tùy chọn]
```

### Các Lệnh Thực Tế Khuyên Dùng:

1. **Chạy Mô phỏng FEA Ansys Maxwell Trực tiếp (Production Khuyên dùng)**:
   ```powershell
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --mode ansys --algorithm nsga2 --pop-size 8 --generations 10 --plot-all
   ```

2. **Chạy Bộ Kiểm thử Hệ thống (Built-in Unit Tests - 15 test cases)**:
   ```powershell
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --test
   ```

3. **Chạy Mô phỏng Offline bằng AI Surrogate (Vài giây)**:
   ```powershell
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --mode offline --pop-size 12 --generations 30 --plot-all
   ```

4. **Tiếp tục Chạy từ Checkpoint bị ngắt quãng (`--resume`)**:
   ```powershell
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --resume --mode ansys --generations 20
   ```

5. **Chạy qua Cầu nối MATLAB ActiveX**:
   ```powershell
   python "motor_optimizer_ver5.2(fix lan3)_remote.py" --mode matlab --algorithm nsga2 --pop-size 10 --generations 8
   ```

---

## 📊 Bảng Tham số Command-Line (CLI Options)

| Tham số CLI | Mặc định | Mô tả & Hướng dẫn sử dụng |
|---|---|---|
| `--mode` | `offline` | Chế độ mô phỏng: `ansys` (PyAEDT/ActiveX), `matlab` (MATLAB bridge), `offline`. |
| `--algorithm` | `nsga2` | Thuật toán: `nsga2` (Đa mục tiêu NSGA-II) hoặc `ga` (Di truyền đơn mục tiêu). |
| `--pop-size` | `8` | Kích thước quần thể (Số lượng mẫu đánh giá mỗi thế hệ). |
| `--generations` | `10` | Số lượng thế hệ tối đa. |
| `--crossover` | `0.7` | Xác suất lai ghép (Crossover probability). |
| `--mutation` | `0.2` | Tỷ lệ đột biến gen (Mutation rate per gene). |
| `--w-eff` | `1.0` | Trọng số Hiệu suất trong điểm tổng hợp (Efficiency weight). |
| `--w-ripple` | `1.0` | Trọng số Độ nhấp nhô Momen trong điểm tổng hợp (Torque Ripple weight). |
| `--w-pwr` | `0.5` | Trọng số Mật độ công suất (Power Density weight). |
| `--w-cost` | `0.05` | Trọng số Chi phí vật liệu (Cost penalty weight). |
| `--non-graphical` | `True` | Chạy Ansys Maxwell không giao diện (Headless mode) tiết kiệm RAM/CPU. |
| `--show-gui` | `False` | Hiển thị giao diện Ansys Maxwell (tương đương `--no-non-graphical`). |
| `--resume` | `False` | Khôi phục trạng thái từ checkpoint `optimizer_state.pkl`. |
| `--plot-all` | `False` | Tự động tạo tất cả 4 biểu đồ phân tích & hội tụ khi hoàn thành. |
| `--plot-pareto` | `False` | Tự động tạo biểu đồ 2D Pareto Front. |
| `--test` | `False` | Chạy bộ 15 Unit Tests kiểm thử toàn bộ hệ thống rồi thoát. |
| `--no-ml` | `False` | Tắt mô hình AI Surrogate, buộc mô phỏng FEA 100%. |
| `--no-local-search` | `False` | Tắt tính năng tìm kiếm cục bộ Elite. |
| `--no-report` | `False` | Bỏ qua việc tạo báo cáo Markdown (`optimization_report.md`). |
| `--keep-temp` | `False` | Giữ lại các file dự án `.aedt` tạm thời sau khi hoàn tất. |
| `--seed` | `None` | Hạt giống ngẫu nhiên (Random seed) để tái lập kết quả. |

---

## 📂 Cấu trúc Thư mục Kết quả Đầu ra (`outputs/`)

Tất cả kết quả của mỗi lần chạy được lưu tự động vào thư mục:
`outputs/run_YYYYMMDD_HHMMSS/` (được liên kết bởi `outputs/latest/`)

Các tệp kết quả bao gồm:
- `best_optimized_design_v5.2.csv`: Thông số 19 biến và chỉ số của thiết kế tối ưu nhất.
- `simulation_history.csv`: Lịch sử đầy đủ của toàn bộ cá thể đã đánh giá.
- `log_history.csv`: Nhật ký chi tiết kết quả mô phỏng và vi phạm ràng buộc.
- `optimizer.log`: Nhật ký thực thi hệ thống chi tiết theo thời gian.
- `optimization_report.md`: Báo cáo tổng quan dạng Markdown tự động tạo.
- `pareto_front.png`: Đồ thị Pareto 2D (Hiệu suất vs Độ nhấp nhô Momen).
- `pareto_3d.png`: Đồ thị Pareto 3D (Hiệu suất vs Độ nhấp nhô Momen vs Chi phí).
- `parallel_coordinates.png`: Đồ thị Tọa độ Song song 4 mục tiêu.
- `convergence_history.png`: Đồ thị Hội tụ 4D Hypervolume & Best Score qua các thế hệ.

---
