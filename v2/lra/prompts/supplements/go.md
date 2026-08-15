# Go 高频陷阱

## 空指针与错误处理
- 对可能为 nil 的指针/接口/切片/映射解引用前必须判空
- `err != nil` 后继续使用返回值（未 return/continue）
- 多返回值中忽略 error（`val, _ := f()`）

## 并发
- goroutine 泄漏：channel 无接收方/发送方永远阻塞
- 循环变量捕获：`for _, v := range s { go func() { use(v) }() }` 共享同一个 v（Go <1.22）
- 未加锁读写 map（并发写 map 直接 panic）
- `sync.WaitGroup.Add()` 在 goroutine 内部调用（竞态）

## 切片与数组
- `append` 可能共享底层数组：`b := a[1:3]; b = append(b, x)` 意外修改 a
- 切片是引用，函数内 append 不影响外部（除非共享底层且未扩容）
- `nil` 切片可以 append，但 `nil` map 赋值会 panic

## defer 与资源
- `defer f.Close()` 在 err 检查之前（f 可能是 nil）
- 循环内 defer 不释放（defer 在函数退出才执行）
- `defer` 参数在 defer 语句时求值，不是执行时

## 接口
- 隐式实现：方法集不匹配时编译器不报错，运行时 nil 接口调用 panic
- `interface{} == nil` 陷阱：带类型的 nil 指针装入接口后 `!= nil`
