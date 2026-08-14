# C/C++ 高频陷阱

## 内存安全
- 悬垂指针：释放后使用（use-after-free）
- 双重释放（double free）
- 缓冲区溢出：数组越界写（`buf[size] = x`，有效索引 0..size-1）
- `malloc`/`new` 后未检查返回值（C 中 malloc 可能返回 NULL）
- 内存泄漏：分配后所有路径未释放（特别是异常/提前 return 路径）

## 未定义行为（UB）
- 有符号整数溢出
- 严格别名违规：通过不兼容指针类型访问对象
- 序列点之间多次修改同一变量（`i = i++ + ++i`）
- 未初始化变量读取
- 空指针解引用 / 空指针算术

## RAII 与资源
- 裸 `new`/`delete` 未用智能指针包裹（应用 unique_ptr/shared_ptr）
- 异常安全：`new` 抛异常前已获取的资源未释放
- 文件描述符/fd 泄漏（`open` 后非所有路径 `close`）

## 并发
- 数据竞争：无锁并发读写非 atomic 变量
- 死锁：多锁获取顺序不一致
- `std::shared_ptr` 跨线程拷贝需 atomic 操作（引用计数非线程安全的场景）

## 常见逻辑错误
- `sizeof(ptr)` vs `sizeof(*ptr)`：指针大小 vs 对象大小
- `strlen` 不含 '\0'，`strcpy` 需要 +1 空间
- 宏定义缺少括号：`#define MUL(a,b) a*b` → `MUL(1+2,3)` = 7 而非 9
- 虚析构函数缺失：基类指针 delete 派生类对象（UB）
