#ifndef SECRETS_H
#define SECRETS_H

// ==========================================
// CẤU HÌNH WIFI
// ==========================================
#define WIFI_SSID "VIETTEL_4A6420"
#define WIFI_PASSWORD "123456a@"

// ==========================================
// CẤU HÌNH FIREBASE
// ==========================================

// 1. API Key: Lấy ở Firebase Console -> Project Settings (biểu tượng bánh răng) 
// -> Tab General -> Kéo xuống phần "Your apps" -> Web API Key
#define FIREBASE_API_KEY "AIzaSyB6AMuCnjdaLoLuWzBYE3lUidORsZJ3YRw" 

// 2. Tài khoản đăng nhập (Bạn phải tạo tài khoản này trong Firebase Authentication)
#define FIREBASE_USER_EMAIL "esp32@gmail.com"
#define FIREBASE_USER_PASSWORD "12345678"

// 3. Đường dẫn Realtime Database: Lấy ở Firebase Console -> Realtime Database
// -> Nhìn lên trên cùng, copy đường link (VD: https://ten-project-default-rtdb.firebaseio.com)
// Lưu ý: KHÔNG có dấu "/" ở cuối
#define FIREBASE_DB_URL "https://khoitrongham-default-rtdb.asia-southeast1.firebasedatabase.app"

#endif