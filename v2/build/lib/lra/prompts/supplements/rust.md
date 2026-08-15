# Rust 高频陷阱

## Option/Result 处理
- `unwrap()` / `expect()` 在生产代码中滥用（应 match 或 `?` 传播）
- `if let Some(x) = opt {}` 后忘记处理 None 分支
- `Result` 被忽略（`let _ = f();`）导致错误静默丢失

## 所有权与借用
- move 后使用：`let b = a; use(a);`（编译错误但逻辑设计问题）
- 迭代器消费集合后再次使用（`for x in vec` 后 vec 已 move）
- 生命周期标注错误导致悬垂引用

## 并发
- `Mutex.lock().unwrap()` 在 panic 后 poison（应处理 PoisonError）
- `Arc<Mutex<T>>` 死锁：嵌套锁顺序不一致
- `Send`/`Sync` trait 未正确实现导致数据竞争

## 集合与迭代
- `Vec` 在迭代中修改（借用检查器会拦，但 `RefCell` 绕过时运行时 panic）
- `HashMap` 的 `entry` API 误用：`or_insert` 每次都计算默认值（应用 `or_insert_with`）
- 整数溢出：release 模式静默回绕，debug 模式 panic

## 类型系统
- `as` 强转截断（`u64 as u32` 静默丢高位）
- trait object (`dyn Trait`) 与泛型 (`impl Trait`) 混用导致对象不安全
- `Clone` 与 `Copy` 语义混淆：以为拷贝了实际是引用
