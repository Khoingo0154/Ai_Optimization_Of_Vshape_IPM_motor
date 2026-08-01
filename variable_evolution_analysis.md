# Phân tích Chi tiết Cơ chế Tiến hóa Biến (Variable Evolution Analysis)

Tài liệu này phân tích chi tiết mã nguồn `motor_optimizer_ver5.1_remote.py` nhằm giải thích cách 19 biến thiết kế motor được khởi tạo ở **Thế hệ 1 (Generation 1)**, cách chúng biến đổi ở **Thế hệ 2 (Generation 2)**, và nguyên lý toán học / thuật toán đằng sau từng bước tiến hóa.

---

## 1. Danh sách 19 Biến Thiết kế (19 Design Variables)

Thuật toán tối ưu điều khiển 19 biến hình học được quy định trong `Ai_Optimization_Bounds.xlsx`:

| STT | Tên biến (`Variable Name`) | Ý nghĩa vật lý (`Physical Meaning`) | Giới hạn (`Bounds`) | Bước nhảy (`Step Size`) |
|---|---|---|---|---|
| 1 | `Dr_in` | Đường kính trong rotor (Inner Rotor Diameter) | 50 – 90 mm | 5.0 mm |
| 2 | `Air_gap` | Khe hở không khí rotor-stator (Air Gap) | 0.5 – 1.5 mm | 0.1 mm |
| 3 | `Lamda` | Tỷ lệ Stack Length / Stator Bore (Lamda Ratio) | 0.8 – 1.0 | 0.1 |
| 4 | `Bridge` | Khoảng cách rotor ngoài - lỗ nam châm (Magnet Bridge) | 1.0 – 3.0 mm | 0.1 mm |
| 5 | `Hs0` | Chiều cao rãnh răng stator (Slot Opening Height) | 1.0 – 2.0 mm | 0.1 mm |
| 6 | `Hs1` | Độ nghiêng rãnh stator (Slot Wedge Height) | 1.0 – 2.0 mm | 0.1 mm |
| 7 | `Hs2` | Chiều cao rãnh chính stator (Slot Body Height) | 16.0 – 35.0 mm | 1.0 mm |
| 8 | `Bs0` | Bề rộng miệng rãnh stator (Slot Opening Width) | 1.5 – 4.0 mm | 0.5 mm |
| 9 | `Bs1` | Bề rộng rãnh dưới stator (Slot Bottom Width) | 3.0 – 10.0 mm | 0.5 mm |
| 10 | `Bs2` | Bề rộng rãnh trên stator (Slot Top Width) | 5.0 – 14.0 mm | 1.0 mm |
| 11 | `O1` | Khoảng cách đáy ống dẫn (Duct Bottom Offset) | 0.0 – 13.0 mm | 1.0 mm |
| 12 | `O2` | Khoảng cách ống dẫn từ rotor trong (Duct Inner Offset) | 2.0 – 7.0 mm | 0.5 mm |
| 13 | `B1` | Bề dày ống dẫn từ nam châm (Duct Thickness) | 3.2 – 6.0 mm | 0.5 mm |
| 14 | `rib` | Bề rộng xương sườn (Rib Width) | 2.0 – 15.0 mm | 1.0 mm |
| 15 | `hrib` | Chiều cao xương sườn (Rib Height) | 2.0 – 6.0 mm | 0.5 mm |
| 16 | `Mt` | Bề dày nam châm (Magnet Thickness) | 4.0 – 6.0 mm | 0.2 mm |
| 17 | `Mw` | Bề rộng nam châm (Magnet Width) | 10.0 – 30.0 mm | 2.0 mm |
| 18 | `magDmin` | Khoảng cách tối thiểu giữa các nam châm (Min Magnet Distance) | 0.0 – 10.0 mm | 1.0 mm |
| 19 | `thet_deg` | Góc pha dòng kích thích (Current Advance Angle) | 0 – 90 deg | 1.0 deg |

---

## 2. Luồng Khởi tạo ở Thế hệ 1 (Generation 1 Initialization)

### 2.1. Công thức tính giá trị ngẫu nhiên (Discrete Random Generation)
Mỗi biến $x_i$ được khởi tạo ngẫu nhiên trên lưới bước nhảy hợp lệ qua hàm `snap_to_step`:

$$k = \text{random\_int}\left(0, \frac{\text{Upper}_i - \text{Lower}_i}{\text{Step}_i}\right)$$

$$x_i = \text{Lower}_i + k \times \text{Step}_i$$

### 2.2. Ép buộc 4 Ràng buộc Hình học Strict (Geometric Constraints)
1. **Ràng buộc chiều cao rãnh (Slot Height Limit):** $Hs_0 + Hs_1 + Hs_2 < \frac{Ds_{out} - Ds_{in}}{2} - 12.25$
2. **Ràng buộc bề dày ống dẫn (Bridge Thickness Limit):** $B_1 \le Mt - 0.3$
3. **Ràng buộc kích thước rotor (Rotor Fits Stator):** $Dr_{out} > Dr_{in} \quad (Dr_{out} = Ds_{in} - 2 \cdot Air\_gap)$
4. **Ràng buộc hình học nam châm V-shape (Magnet Duct Fit):** $Mw > 2 \cdot B_1$

Nếu ngẫu nhiên sinh ra cá thể vi phạm, hàm Sửa lỗi Thông minh (`repair_individual`) sẽ tự động giảm các chiều cao $Hs$, giảm bề dày $B_1$, hoặc giảm $Dr_{in}$ theo nấc bước nhảy cho đến khi hợp lệ.

### 2.3. Từng bước Tính toán Chi tiết ra Kết quả Gen 1 - Sol 1 (Cá thể A)

Lấy từng thông số của **Cá thể A (Gen 1 - Phương án 1 / Solution 1)** làm ví dụ tính toán thực tế:

#### Bước 1: Tính toán giá trị từng biến theo nấc bước nhảy
- **Biến `Dr_in` (Giới hạn 50 – 90 mm, Step = 5.0 mm):**
  $$\text{Số nấc khả dụng} = \frac{90 - 50}{5} = 8 \text{ nấc}$$
  Code sinh số ngẫu nhiên $k = 4 \rightarrow \mathbf{Dr\_in} = 50 + 4 \times 5.0 = \mathbf{70.0 \text{ mm}}$.
- **Biến `Air_gap` (Giới hạn 0.5 – 1.5 mm, Step = 0.1 mm):**
  $$\text{Số nấc khả dụng} = \frac{1.5 - 0.5}{0.1} = 10 \text{ nấc}$$
  Code sinh số ngẫu nhiên $k = 5 \rightarrow \mathbf{Air\_gap} = 0.5 + 5 \times 0.1 = \mathbf{1.0 \text{ mm}}$.
- **Biến `Mt` (Giới hạn 4.0 – 6.0 mm, Step = 0.2 mm):**
  Code sinh ngẫu nhiên $k = 5 \rightarrow \mathbf{Mt} = 4.0 + 5 \times 0.2 = \mathbf{5.0 \text{ mm}}$.
- **Biến `B1` (Giới hạn 3.2 – 6.0 mm, Step = 0.5 mm):**
  Code sinh ngẫu nhiên $k = 1 \rightarrow \mathbf{B1} = 3.2 + 1 \times 0.5 = \mathbf{3.7 \text{ mm}}$.
  - *Kiểm tra Ràng buộc 2 ($B_1 \le Mt - 0.3$):* $3.7 \le 5.0 - 0.3 = 4.7$ (Thỏa mãn hợp lệ!).

#### Bước 2: Ansys mô phỏng & Công thức tính điểm Composite Score (`compute_score`)
Giả sử Ansys mô phỏng Cá thể A trả về các thông số: Hiệu suất `Eff = 94.5%`, Nhấp nhô `Ripple = 11.2%`, Mật độ công suất `PowerDensity = 3.8 kW/kg`, Chi phí `Cost = $130.0`.

$$\text{Score} = (w_{eff} \cdot \text{Eff}) - (w_{ripple} \cdot \text{Ripple}) + (w_{pwr} \cdot \text{Pwr}) - \left(w_{cost} \cdot \frac{\text{Cost}}{150}\right)$$

$$\text{Score} = (1.0 \times 94.5) - (1.0 \times 11.2) + (0.5 \times 3.8) - \left(0.05 \times \frac{130}{150}\right) = 94.5 - 11.2 + 1.9 - 0.043 = \mathbf{+85.157}$$

---

### 2.4. Làm sao để xác định được "Bố A" và "Mẹ B" từ Thế hệ 1?

Ở Thế hệ 1, giả sử bạn thiết lập quần thể `--pop-size 8` (gồm 8 phương án từ Solution 1 đến Solution 8). Sau khi mô phỏng xong, phần mềm thực hiện chọn lọc:

1. **Xếp hạng danh sách kết quả Lần 1 (Ranking):**
   - `Sol 1`: Score = **+85.15** $\rightarrow$ **Hạng 1 (Best Individual)**
   - `Sol 4`: Score = **+78.40** $\rightarrow$ **Hạng 2**
   - `Sol 7`: Score = **+72.10** $\rightarrow$ **Hạng 3**
   - ... `Sol 8`: Score = **+40.00** $\rightarrow$ **Hạng 8 (Worst Individual)**

2. **Cách chọn Bố A (`nsga2_tournament_selection` / Binary Tournament Selection):**
   - Thuật toán bốc thăm ngẫu nhiên 2 phương án bất kỳ trong 8 phương án (ví dụ bốc trúng `Sol 1` và `Sol 8`).
   - `Sol 1` (+85.15) đấu với `Sol 8` (+40.00) $\rightarrow$ `Sol 1` thắng! $\rightarrow$ **`Sol 1` chính là Bố A (Parent A)**.

3. **Cách chọn Mẹ B:**
   - Thuật toán bốc thăm tiếp 2 phương án khác (ví dụ bốc trúng `Sol 4` và `Sol 7`).
   - `Sol 4` (+78.40) đấu với `Sol 7` (+72.10) $\rightarrow$ `Sol 4` thắng! $\rightarrow$ **`Sol 4` chính là Mẹ B (Parent B)**.

4. **Kết quả:** Bố A (`Sol 1`) và Mẹ B (`Sol 4`) được đưa vào máy lai ghép để sinh ra đứa con mới cho Thế hệ 2.

---

## 3. Giải thích Khái niệm & Cơ chế Tiến hóa sang Thế hệ 2 (Generation 2)

### 3.1. Các Khái niệm Cơ bản (Core Terminology)
Để dễ hình dung thuật toán di truyền (Genetic Algorithm - GA / NSGA-II) áp dụng cho thiết kế motor:
- **Cá thể (Individual / Candidate Solution):** Là **một phương án thiết kế motor hoàn chỉnh** gồm đầy đủ 19 thông số kỹ thuật (đường kính, bề dày nam châm, góc pha, v.v.).
- **Gen (Gene):** Là **giá trị của một thông số đơn lẻ** trong bản thiết kế (ví dụ: `Dr_in = 70.0mm` là 1 gen, `Mt = 5.0mm` là 1 gen).
- **Lần 1 Bố / Lần 1 Mẹ (Parent A & Parent B ở Generation 1):**
  - Khi chạy Lần 1, phần mềm tạo ra một tập hợp $N$ thiết kế motor ngẫu nhiên và đem đi mô phỏng Ansys.
  - Ansys trả về kết quả (Hiệu suất, Nhấp nhô mô-men) và Python tính ra điểm số (`Score`) cho từng thiết kế.
  - Thuật toán chọn ra **2 thiết kế có kết quả xuất sắc nhất ở Lần 1** để chuẩn bị phối giống. Thiết kế xuất sắc thứ nhất gọi là **"Bố A" (Parent A)**, thiết kế xuất sắc thứ hai gọi là **"Mẹ B" (Parent B)**.

---

### 3.2. Bốn Bước Tiến hóa từ Lần 1 sang Lần 2 (Evolutionary Operators)

#### Bước 1: Chọn lọc & Bảo tồn Ưu tú (Selection & Elitism)
- **Bảo tồn ưu tú (Elitism / Elite Preservation):** Thiết kế tốt nhất Lần 1 (Bố A với `Score = +84.2`) được **giữ nguyên 100%** đưa thẳng sang Lần 2 để đảm bảo kết quả không bao giờ bị thụt lùi.
- **Chọn lọc bố mẹ (Binary Tournament Selection):** Tổ chức "đấu trường" chọn ra cặp Bố A và Mẹ B để tiến hành lai ghép sinh ra các thiết kế con mới cho Lần 2.

#### Bước 2: Lai ghép Đồng nhất (Uniform Crossover - Prob = 0.7)
- Đứa con ở Lần 2 sẽ được thừa hưởng **50% đặc điểm từ Bố A** và **50% đặc điểm từ Mẹ B**.
- Với từng biến trong 19 biến, phần mềm tung đồng xu (xác suất 50/50):
  $$\text{Gen\_Con}_i = \begin{cases} \text{Gen\_Bố}_i & \text{nếu ngửa } (\le 0.5) \\ \text{Gen\_Mẹ}_i & \text{nếu sấp } (> 0.5) \end{cases}$$

#### Bước 3: Đột biến Bước nhảy (Step Offset Mutation - Prob = 0.2/gene)
- Sau khi đứa con gom đủ 19 gen từ Bố và Mẹ, mỗi gen có **20% khả năng bị biến đổi ngẫu nhiên** nhích lên hoặc lùi xuống $s \in \{-3, -2, -1, 1, 2, 3\}$ nấc bước nhảy.
- Giúp thiết kế con phát hiện ra các vùng thông số mới tốt hơn mà Bố/Mẹ chưa từng có.

#### Bước 4: Sửa lỗi Thông minh (Smart Repair Function)
- Nếu việc kết hợp gen giữa Bố & Mẹ hoặc đột biến vô tình làm cho motor vi phạm hình học (ví dụ: bề dày ống dẫn `B1` bị đẩy lên cao hơn bề dày nam châm `Mt`), hàm `repair_individual()` sẽ tự động kéo `B1` lùi xuống từng nấc bước nhảy cho đến khi hợp lệ.

---

## 4. Bảng Ví dụ Minh họa Trực quan Biến đổi từ Lần 1 sang Lần 2

Dưới đây là ví dụ cụ thể cách một cá thể Con ở **Lần 2** được tạo thành từ **Bố A (Lần 1)** và **Mẹ B (Lần 1)**:

| Biến hình học (`Design Variable`) | Lần 1: Bố A (`Parent A`) | Lần 1: Mẹ B (`Parent B`) | Lần 2: Con lai thu được (`Child Solution`) | Quá trình biến đổi & Nguyên nhân |
|---|---|---|---|---|
| `Dr_in` (Đường kính rotor) | **70.0 mm** | 85.0 mm | **70.0 mm** | **Kế thừa từ Bố A:** Uniform Crossover chọn gen từ Bố A. |
| `Air_gap` (Khe hở) | 1.0 mm | **1.2 mm** | **1.2 mm** | **Kế thừa từ Mẹ B:** Uniform Crossover chọn gen từ Mẹ B. |
| `Lamda` (Tỷ lệ chiều dài) | **0.9** | 0.8 | **0.9** | **Kế thừa từ Bố A:** Uniform Crossover chọn gen từ Bố A. |
| `B1` (Bề dày ống dẫn) | 3.5 mm | **4.0 mm** | **4.0 mm** | **Kế thừa từ Mẹ B:** Uniform Crossover chọn gen từ Mẹ B. |
| `Mt` (Bề dày nam châm) | **5.0 mm** | 5.4 mm | **5.0 mm** | **Kế thừa từ Bố A:** Uniform Crossover chọn gen từ Bố A. |
| `Mw` (Rộng nam châm) | 24.0 mm | 20.0 mm | **26.0 mm** | **Kế thừa từ Bố A (24.0mm) + Step Offset Mutation:** Gen bị đột biến $+1$ bước nhảy ($+2.0\text{mm}) \rightarrow 24.0 + 2.0 = 26.0\text{mm}$. |
| `thet_deg` (Góc pha) | 30.0 deg | 45.0 deg | **32.0 deg** | **Kế thừa từ Bố A (30.0deg) + Step Offset Mutation:** Gen bị đột biến $+2$ bước nhảy ($+2.0\text{deg}) \rightarrow 30.0 + 2.0 = 32.0\text{deg}$. |

---

## 5. Mô hình Học máy Surrogate (Machine Learning Gaussian Process Regression - GPR)
Sau Lần 1, mô hình ML `GaussianProcessRegressor` (hoặc `KNN-IDW` - K-Nearest Neighbors Inverse Distance Weighting) nhận $N$ mẫu dữ liệu thực tế từ Lần 1 để chuẩn hóa biến về $[0, 1]$ (**Input Standardization / Normalization**) và học ma trận tương quan. Sang Lần 2, nếu chạy chế độ `offline`, ML sẽ dự đoán nhanh hiệu suất của các bộ biến Lần 2 trước khi gửi sang FEM.

---

## 6. Cơ chế Tăng / Giảm / Giữ Nguyên Biến & Phân tích Độ nhạy Spearman

### 6.1. Hỗ trợ đầy đủ cả 3 hướng thay đổi biến (Increase, Decrease, Hold)
Các biến trong thuật toán **KHÔNG BỊ ÉP CHỈ TĂNG DẦN**, mà được tự do biến đổi theo 3 hướng:
1. **GIẢM BIẾN (Decrease - Shift âm):** Khi đột biến chọn $s \in \{-3, -2, -1\}$, giá trị biến bị lùi âm $-1, -2, -3$ nấc bước nhảy.
   - *Ví dụ:* `Dr_in` đang là `80.0mm` (bước 5mm) dịch $-1$ nấc $\rightarrow$ **`75.0mm`**.
2. **TĂNG BIẾN (Increase - Shift dương):** Khi đột biến chọn $s \in \{1, 2, 3\}$, giá trị biến được cộng tiến $+1, +2, +3$ nấc bước nhảy.
   - *Ví dụ:* `Mt` đang là `5.0mm` (bước 0.2mm) dịch $+2$ nấc $\rightarrow$ **`5.4mm`**.
3. **GIỮ NGUYÊN BIẾN (Hold - Không đột biến):** Với xác suất 80% không bị đột biến, biến **giữ nguyên 100%** giá trị xuất sắc kế thừa từ Bố/Mẹ.

### 6.2. Thuật toán tự tìm ra xu hướng "Tăng A, Giữ B, Giảm C" bằng cách nào? (Survival of the Fittest)
Thuật toán Di truyền (Genetic Algorithm - GA / NSGA-II) sử dụng cơ chế **Chọn lọc tự nhiên (Survival of the Fittest)**:
- **Khám phá ngẫu nhiên đa chiều (Multidimensional Exploration):** Qua lai ghép và đột biến, thuật toán tự động sinh ra các kết hợp như:
  - *Cá thể 1:* Tăng A, Giữ B, Giảm C $\rightarrow$ Đạt yêu cầu vật lý $\rightarrow$ **Score vượt trội (+88.5)**.
  - *Cá thể 2:* Giảm A, Tăng B, Tăng C $\rightarrow$ Sai hướng vật lý $\rightarrow$ **Score thấp (+62.0)**.
- **Đào thải & Nhân bản (Selection & Pruning):** Bước chọn lọc (Binary Tournament Selection / Fast Non-Dominated Sorting) sẽ **giữ lại Cá thể 1** làm Bố/Mẹ cho thế hệ sau và **đào thải hoàn toàn Cá thể 2**.

### 6.3. Phân tích độ nhạy Spearman (Spearman Rank Correlation Sensitivity Analysis `--sensitivity`)
Hệ thống hỗ trợ cờ lệnh `--sensitivity` để tính toán hệ số tương quan thứ hạng Spearman ($r \in [-1, 1]$) giữa từng biến với Score:
- **Hệ số dương ($r > 0$ - Positive Correlation):** Muốn Score cao thì biến đó **NÊN TĂNG** (Increase).
- **Hệ số âm ($r < 0$ - Negative Correlation):** Muốn Score cao thì biến đó **NÊN GIẢM** (Decrease).
- **Hệ số xấp xỉ 0 ($r \approx 0$ - Neutral Correlation):** Biến ít ảnh hưởng, **NÊN GIỮ NGUYÊN** (Hold) ở khoảng an toàn.
