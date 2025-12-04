#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/sysinfo.h>
#include <fcntl.h>

#define BROADCAST_PORT 9999
#define BROADCAST_IP "255.255.255.255" // 或者你的局域网广播地址，如 192.168.1.255
#define BUFFER_SIZE 2048
#define INTERVAL_SECONDS 2

// 辅助函数：读取文件内容的一行
int read_file_line(const char *path, char *buffer, size_t size)
{
    FILE *fp = fopen(path, "r");
    if (!fp)
        return 0;
    if (fgets(buffer, size, fp) != NULL)
    {
        // 去除换行符
        buffer[strcspn(buffer, "\n")] = 0;
        fclose(fp);
        return 1;
    }
    fclose(fp);
    return 0;
}

// 辅助函数：获取特定格式的 CMM Total Size
// 目标格式: ... total size=12582912KB(12288MB) ...
void get_cmm_total(char *output, size_t size)
{
    FILE *fp = fopen("/proc/ax_proc/mem_cmm_info", "r");
    if (!fp)
    {
        snprintf(output, size, "N/A");
        return;
    }

    char line[512];
    int found = 0;
    while (fgets(line, sizeof(line), fp))
    {
        char *ptr = strstr(line, "total size=");
        if (ptr)
        {
            ptr += strlen("total size="); // 移动指针到数字开始处
            // 提取直到逗号或空格的内容
            int i = 0;
            while (ptr[i] != ',' && ptr[i] != '\0' && ptr[i] != '\n' && i < size - 1)
            {
                output[i] = ptr[i];
                i++;
            }
            output[i] = '\0';
            found = 1;
            break;
        }
    }
    fclose(fp);
    if (!found)
        snprintf(output, size, "Unknown");
}

// 辅助函数：计算 CPU 使用率
// 读取 /proc/stat 计算瞬间占用
unsigned long long last_total_user = 0, last_total_user_low = 0, last_total_sys = 0, last_total_idle = 0;

double get_cpu_usage()
{
    FILE *file = fopen("/proc/stat", "r");
    if (!file)
        return 0.0;

    unsigned long long user, nice, system, idle;
    char buffer[1024];
    if (!fgets(buffer, sizeof(buffer), file))
    {
        fclose(file);
        return 0.0;
    }

    sscanf(buffer, "cpu %llu %llu %llu %llu", &user, &nice, &system, &idle);
    fclose(file);

    if (last_total_user == 0 && last_total_idle == 0)
    {
        // 第一次读取，无法计算差值，先保存并返回 0
        last_total_user = user;
        last_total_user_low = nice;
        last_total_sys = system;
        last_total_idle = idle;
        return 0.0;
    }

    unsigned long long total_user = user - last_total_user;
    unsigned long long total_user_low = nice - last_total_user_low;
    unsigned long long total_system = system - last_total_sys;
    unsigned long long total_idle = idle - last_total_idle;

    last_total_user = user;
    last_total_user_low = nice;
    last_total_sys = system;
    last_total_idle = idle;

    unsigned long long total = total_user + total_user_low + total_system + total_idle;
    if (total == 0)
        return 0.0;

    return (1.0 - (double)total_idle / total) * 100.0;
}

// 获取系统内存信息 (MemTotal, MemFree)
void get_sys_mem(long *total_kb, long *free_kb)
{
    FILE *fp = fopen("/proc/meminfo", "r");
    if (!fp)
        return;
    char line[256];
    *total_kb = 0;
    *free_kb = 0;
    while (fgets(line, sizeof(line), fp))
    {
        if (strncmp(line, "MemTotal:", 9) == 0)
        {
            sscanf(line, "MemTotal: %ld kB", total_kb);
        }
        else if (strncmp(line, "MemFree:", 8) == 0)
        {
            sscanf(line, "MemFree: %ld kB", free_kb);
        }
    }
    fclose(fp);
}

// [扩展接口] 在这里添加你想广播的其他信息
void append_custom_info(char *json_buffer, size_t max_len)
{
    // 示例：添加密码字段（虽然局域网广播明文密码不推荐，但按你要求预留）
    // char password[] = "123456";
    // char temp[128];
    // snprintf(temp, sizeof(temp), ",\"password\":\"%s\"", password);
    // strncat(json_buffer, temp, max_len - strlen(json_buffer) - 1);

    // 你可以在这里添加更多字段，注意 JSON 格式逗号
}

int main()
{
    int sockfd;
    struct sockaddr_in broadcast_addr;
    int broadcast_enable = 1;
    char json_buffer[BUFFER_SIZE];

    // 基础信息缓存
    char uid[128], version[128], board_id[128], cmm_info[128], user_name[64];

    // 创建 UDP socket
    if ((sockfd = socket(AF_INET, SOCK_DGRAM, 0)) < 0)
    {
        perror("Socket creation failed");
        return 1;
    }

    // 设置广播权限
    if (setsockopt(sockfd, SOL_SOCKET, SO_BROADCAST, &broadcast_enable, sizeof(broadcast_enable)) < 0)
    {
        perror("Error in setting Broadcast option");
        return 1;
    }

    memset(&broadcast_addr, 0, sizeof(broadcast_addr));
    broadcast_addr.sin_family = AF_INET;
    broadcast_addr.sin_port = htons(BROADCAST_PORT);
    broadcast_addr.sin_addr.s_addr = inet_addr(BROADCAST_IP);

    printf("Starting broadcast on port %d...\n", BROADCAST_PORT);

    while (1)
    {
        // 1. 获取各项信息
        read_file_line("/proc/ax_proc/uid", uid, sizeof(uid));
        // 这里的 UID 输出带 "ax_uid: " 前缀，我们跳过它
        char *uid_clean = strstr(uid, "0x");
        if (!uid_clean)
            uid_clean = uid;

        read_file_line("/proc/ax_proc/version", version, sizeof(version));
        read_file_line("/proc/ax_proc/board_id", board_id, sizeof(board_id));
        get_cmm_total(cmm_info, sizeof(cmm_info)); // 解析 CMM

        // 获取系统内存
        long mem_total, mem_free;
        get_sys_mem(&mem_total, &mem_free);

        // 获取用户名
        char *env_user = getenv("USER");
        snprintf(user_name, sizeof(user_name), "%s", env_user ? env_user : "root");

        // 获取 CPU 占用
        double cpu_usage = get_cpu_usage();

        // 2. 组装 JSON 字符串
        // 注意：嵌入式环境如果没有 json 库，手写是最快的。
        snprintf(json_buffer, BUFFER_SIZE,
                 "{"
                 "\"uid\": \"%s\","
                 "\"version\": \"%s\","
                 "\"board_id\": \"%s\","
                 "\"cmm_total\": \"%s\","
                 "\"sys_mem_total_kb\": %ld,"
                 "\"sys_mem_free_kb\": %ld,"
                 "\"cpu_usage_percent\": %.2f,"
                 "\"user\": \"%s\""
                 // 注意：下面的 append_custom_info 负责添加后续内容，如果后面没内容，这里不需要逗号
                 // 为了简化 JSON 拼接，这里先闭合，通过字符串操作插入
                 "}",
                 uid_clean, version, board_id, cmm_info, mem_total, mem_free, cpu_usage, user_name);

        // 如果有扩展信息，去掉最后的 '}' 并追加
        // 这是一个简单的 hack，为了不引入复杂的 JSON 库
        size_t len = strlen(json_buffer);
        json_buffer[len - 1] = '\0'; // 去掉 '}'
        append_custom_info(json_buffer, BUFFER_SIZE);
        strcat(json_buffer, "}"); // 补回 '}'

        // 3. 发送广播
        if (sendto(sockfd, json_buffer, strlen(json_buffer), 0, (struct sockaddr *)&broadcast_addr, sizeof(broadcast_addr)) < 0)
        {
            perror("Broadcast send failed");
        }
        else
        {
            printf("Sent: %s\n", json_buffer); // 调试用
        }

        sleep(INTERVAL_SECONDS);
    }

    close(sockfd);
    return 0;
}