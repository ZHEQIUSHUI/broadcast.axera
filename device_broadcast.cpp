#include <arpa/inet.h>
#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <dirent.h>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <pwd.h>
#include <sstream>
#include <string>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <unistd.h>
#include <vector>

namespace {

const int kDefaultBroadcastPort = 9999;
const char *kDefaultBroadcastIp = "255.255.255.255";
const int kDefaultIntervalSeconds = 2;
const size_t kBufferSize = 8192;

struct CpuStat {
    unsigned long long user = 0;
    unsigned long long nice = 0;
    unsigned long long system = 0;
    unsigned long long idle = 0;
    unsigned long long iowait = 0;
    unsigned long long irq = 0;
    unsigned long long softirq = 0;
    unsigned long long steal = 0;
};

struct CpuSample {
    CpuStat stat;
    bool valid = false;
};

struct MemoryMetrics {
    long total_kb = -1;
    long available_kb = -1;
    long free_kb = -1;
    long used_kb = -1;
    long buffers_kb = -1;
    long cached_kb = -1;
    long sreclaimable_kb = -1;
    long shmem_kb = -1;
    long cache_effective_kb = -1;
    double used_percent = -1.0;
};

struct CmmMetrics {
    long total_kb = -1;
    long free_kb = -1;
    long used_kb = -1;
    double used_percent = -1.0;
};

struct GpuMetrics {
    bool present = false;
    std::string vendor = "";
    long total_mb = -1;
    long used_mb = -1;
    double used_percent = -1.0;
    double core_usage_percent = -1.0;
    std::string note = "";
};

struct DeviceInfo {
    std::string schema_version = "2";
    std::string hostname = "";
    std::string user = "";
    std::string device_type = "Generic Linux";
    std::string device_kind = "generic_linux";
    std::string platform_vendor = "Generic";
    std::string board_model = "";
    std::string board_vendor = "";
    std::string machine = "";
    std::string arch = "";
    std::string os_pretty_name = "";
    std::string os_name = "";
    std::string os_id = "";
    std::string os_like = "";
    std::string os_version = "";
    std::string kernel = "";
    std::string libc = "";
    std::string uid = "";
    std::string version = "";
    std::string board_id = "";
    bool is_ax = false;
    bool is_raspberry_pi = false;
    int cpu_cores = 0;
    long uptime_seconds = -1;
    double cpu_usage_percent = -1.0;
    MemoryMetrics memory;
    GpuMetrics gpu;
    CmmMetrics cmm;
    long long timestamp_ms = 0;
};

std::string trim(const std::string &value) {
    const char *whitespace = " \t\r\n";
    size_t begin = value.find_first_not_of(whitespace);
    if (begin == std::string::npos) {
        return "";
    }
    size_t end = value.find_last_not_of(whitespace);
    return value.substr(begin, end - begin + 1);
}

std::string to_lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

bool file_exists(const std::string &path) {
    struct stat st;
    return stat(path.c_str(), &st) == 0;
}

bool read_file(const std::string &path, std::string *output) {
    std::ifstream stream(path.c_str(), std::ios::in | std::ios::binary);
    if (!stream) {
        return false;
    }
    std::ostringstream buffer;
    buffer << stream.rdbuf();
    *output = buffer.str();
    return true;
}

bool read_first_line(const std::string &path, std::string *output) {
    std::ifstream stream(path.c_str());
    if (!stream) {
        return false;
    }
    std::string line;
    if (!std::getline(stream, line)) {
        return false;
    }
    *output = trim(line);
    return true;
}

std::string strip_after_nul(const std::string &input) {
    size_t pos = input.find('\0');
    if (pos == std::string::npos) {
        return trim(input);
    }
    return trim(input.substr(0, pos));
}

std::string json_escape(const std::string &input) {
    std::ostringstream escaped;
    for (size_t i = 0; i < input.size(); ++i) {
        const unsigned char c = static_cast<unsigned char>(input[i]);
        switch (c) {
            case '\\':
                escaped << "\\\\";
                break;
            case '"':
                escaped << "\\\"";
                break;
            case '\b':
                escaped << "\\b";
                break;
            case '\f':
                escaped << "\\f";
                break;
            case '\n':
                escaped << "\\n";
                break;
            case '\r':
                escaped << "\\r";
                break;
            case '\t':
                escaped << "\\t";
                break;
            default:
                if (c < 0x20) {
                    escaped << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                            << static_cast<int>(c) << std::dec << std::setfill(' ');
                } else {
                    escaped << input[i];
                }
        }
    }
    return escaped.str();
}

bool command_exists(const std::string &command) {
    const char *path_env = getenv("PATH");
    if (!path_env) {
        return false;
    }

    std::string path_value(path_env);
    std::stringstream ss(path_value);
    std::string entry;
    while (std::getline(ss, entry, ':')) {
        if (entry.empty()) {
            continue;
        }
        std::string full_path = entry + "/" + command;
        if (access(full_path.c_str(), X_OK) == 0) {
            return true;
        }
    }
    return false;
}

bool run_command(const std::string &command, std::string *output) {
    FILE *pipe = popen(command.c_str(), "r");
    if (!pipe) {
        return false;
    }

    char buffer[512];
    std::ostringstream result;
    while (fgets(buffer, sizeof(buffer), pipe) != NULL) {
        result << buffer;
    }

    int status = pclose(pipe);
    if (status != 0) {
        return false;
    }

    *output = trim(result.str());
    return true;
}

std::string get_env_or_default(const char *name, const std::string &fallback) {
    const char *value = getenv(name);
    if (!value || value[0] == '\0') {
        return fallback;
    }
    return value;
}

int get_env_int(const char *name, int fallback) {
    const char *value = getenv(name);
    if (!value || value[0] == '\0') {
        return fallback;
    }
    char *end = NULL;
    long parsed = strtol(value, &end, 10);
    if (!end || *end != '\0' || parsed <= 0) {
        return fallback;
    }
    return static_cast<int>(parsed);
}

long long parse_size_to_kb(const std::string &input) {
    std::string value = trim(input);
    if (value.empty()) {
        return -1;
    }

    size_t index = 0;
    while (index < value.size() && (std::isdigit(static_cast<unsigned char>(value[index])) || value[index] == '.')) {
        ++index;
    }
    if (index == 0) {
        return -1;
    }

    double number = atof(value.substr(0, index).c_str());
    std::string unit = to_lower(value.substr(index));
    if (unit.find("kb") != std::string::npos || unit == "k") {
        return static_cast<long long>(number);
    }
    if (unit.find("mb") != std::string::npos || unit == "m") {
        return static_cast<long long>(number * 1024.0);
    }
    if (unit.find("gb") != std::string::npos || unit == "g") {
        return static_cast<long long>(number * 1024.0 * 1024.0);
    }
    if (unit.find("b") != std::string::npos) {
        return static_cast<long long>(number / 1024.0);
    }
    return static_cast<long long>(number);
}

long long extract_size_value_kb(const std::string &line, const std::string &marker) {
    size_t pos = to_lower(line).find(to_lower(marker));
    if (pos == std::string::npos) {
        return -1;
    }

    pos += marker.size();
    while (pos < line.size() && std::isspace(static_cast<unsigned char>(line[pos]))) {
        ++pos;
    }

    size_t end = pos;
    while (end < line.size() && line[end] != ',' && line[end] != ';' && line[end] != '\n' && line[end] != '(') {
        ++end;
    }
    return parse_size_to_kb(line.substr(pos, end - pos));
}

long long extract_any_size_value_kb(const std::string &line, const std::vector<std::string> &markers) {
    for (size_t i = 0; i < markers.size(); ++i) {
        long long value = extract_size_value_kb(line, markers[i]);
        if (value >= 0) {
            return value;
        }
    }
    return -1;
}

bool read_cpu_stat(CpuStat *stat) {
    std::ifstream stream("/proc/stat");
    if (!stream) {
        return false;
    }

    std::string prefix;
    stream >> prefix;
    if (prefix != "cpu") {
        return false;
    }

    stream >> stat->user >> stat->nice >> stat->system >> stat->idle
           >> stat->iowait >> stat->irq >> stat->softirq >> stat->steal;
    return true;
}

double get_cpu_usage(CpuSample *sample) {
    CpuStat current;
    if (!read_cpu_stat(&current)) {
        return -1.0;
    }

    if (!sample->valid) {
        sample->stat = current;
        sample->valid = true;
        return 0.0;
    }

    unsigned long long prev_idle = sample->stat.idle + sample->stat.iowait;
    unsigned long long curr_idle = current.idle + current.iowait;

    unsigned long long prev_non_idle = sample->stat.user + sample->stat.nice + sample->stat.system +
                                       sample->stat.irq + sample->stat.softirq + sample->stat.steal;
    unsigned long long curr_non_idle = current.user + current.nice + current.system +
                                       current.irq + current.softirq + current.steal;

    unsigned long long prev_total = prev_idle + prev_non_idle;
    unsigned long long curr_total = curr_idle + curr_non_idle;

    unsigned long long total_delta = curr_total - prev_total;
    unsigned long long idle_delta = curr_idle - prev_idle;

    sample->stat = current;
    if (total_delta == 0) {
        return 0.0;
    }
    return (1.0 - static_cast<double>(idle_delta) / static_cast<double>(total_delta)) * 100.0;
}

MemoryMetrics get_memory_metrics() {
    MemoryMetrics metrics;
    std::ifstream stream("/proc/meminfo");
    if (!stream) {
        return metrics;
    }

    std::string key;
    long value = 0;
    std::string unit;
    long mem_available_fallback = -1;

    while (stream >> key >> value >> unit) {
        if (key == "MemTotal:") {
            metrics.total_kb = value;
        } else if (key == "MemAvailable:") {
            metrics.available_kb = value;
        } else if (key == "MemFree:") {
            metrics.free_kb = value;
            mem_available_fallback = value;
        } else if (key == "Buffers:") {
            metrics.buffers_kb = value;
        } else if (key == "Cached:") {
            metrics.cached_kb = value;
        } else if (key == "SReclaimable:") {
            metrics.sreclaimable_kb = value;
        } else if (key == "Shmem:") {
            metrics.shmem_kb = value;
        }
    }

    long cache_effective_kb = 0;
    if (metrics.cached_kb > 0) {
        cache_effective_kb += metrics.cached_kb;
    }
    if (metrics.sreclaimable_kb > 0) {
        cache_effective_kb += metrics.sreclaimable_kb;
    }
    if (metrics.shmem_kb > 0) {
        cache_effective_kb -= metrics.shmem_kb;
    }
    if (cache_effective_kb < 0) {
        cache_effective_kb = 0;
    }
    metrics.cache_effective_kb = cache_effective_kb;

    if (metrics.available_kb < 0 && mem_available_fallback >= 0) {
        long fallback = mem_available_fallback;
        if (metrics.buffers_kb > 0) {
            fallback += metrics.buffers_kb;
        }
        if (metrics.cache_effective_kb > 0) {
            fallback += metrics.cache_effective_kb;
        }
        metrics.available_kb = fallback;
    }

    if (metrics.total_kb > 0 && metrics.free_kb >= 0) {
        long working_set_kb = metrics.total_kb - metrics.free_kb;
        if (metrics.buffers_kb > 0) {
            working_set_kb -= metrics.buffers_kb;
        }
        if (metrics.cache_effective_kb > 0) {
            working_set_kb -= metrics.cache_effective_kb;
        }
        if (working_set_kb < 0) {
            working_set_kb = 0;
        }
        metrics.used_kb = working_set_kb;
    } else if (metrics.total_kb > 0 && metrics.available_kb >= 0) {
        metrics.used_kb = metrics.total_kb - metrics.available_kb;
    }

    if (metrics.total_kb > 0 && metrics.used_kb >= 0) {
        metrics.used_percent = static_cast<double>(metrics.used_kb) * 100.0 / static_cast<double>(metrics.total_kb);
    }
    return metrics;
}

GpuMetrics try_read_nvidia_gpu() {
    GpuMetrics metrics;
    if (!command_exists("nvidia-smi")) {
        return metrics;
    }

    std::string output;
    if (!run_command("nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null", &output)) {
        return metrics;
    }

    std::stringstream ss(output);
    std::string line;
    long used_mb_total = 0;
    long total_mb_total = 0;
    double util_sum = 0.0;
    int gpu_count = 0;

    while (std::getline(ss, line)) {
        line = trim(line);
        if (line.empty()) {
            continue;
        }

        std::stringstream line_stream(line);
        std::string used_mb_str;
        std::string total_mb_str;
        std::string util_str;
        if (!std::getline(line_stream, used_mb_str, ',')) {
            continue;
        }
        if (!std::getline(line_stream, total_mb_str, ',')) {
            continue;
        }
        if (!std::getline(line_stream, util_str, ',')) {
            continue;
        }

        used_mb_total += atol(trim(used_mb_str).c_str());
        total_mb_total += atol(trim(total_mb_str).c_str());
        util_sum += atof(trim(util_str).c_str());
        ++gpu_count;
    }

    if (gpu_count == 0 || total_mb_total <= 0) {
        return metrics;
    }

    metrics.present = true;
    metrics.vendor = "NVIDIA";
    metrics.total_mb = total_mb_total;
    metrics.used_mb = used_mb_total;
    metrics.used_percent = static_cast<double>(used_mb_total) * 100.0 / static_cast<double>(total_mb_total);
    metrics.core_usage_percent = util_sum / static_cast<double>(gpu_count);
    return metrics;
}

GpuMetrics try_read_drm_gpu() {
    GpuMetrics metrics;
    DIR *dir = opendir("/sys/class/drm");
    if (!dir) {
        return metrics;
    }

    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        std::string name = entry->d_name;
        if (name.find("card") != 0 || name.find('-') != std::string::npos) {
            continue;
        }

        std::string base = "/sys/class/drm/" + name + "/device";
        std::string total_str;
        std::string used_str;
        std::string busy_str;
        std::string vendor_str;
        long total_mb = -1;
        long used_mb = -1;
        double busy_percent = -1.0;

        if (read_first_line(base + "/mem_info_vram_total", &total_str)) {
            total_mb = static_cast<long>(atoll(total_str.c_str()) / 1024 / 1024);
        }
        if (read_first_line(base + "/mem_info_vram_used", &used_str)) {
            used_mb = static_cast<long>(atoll(used_str.c_str()) / 1024 / 1024);
        }
        if (read_first_line(base + "/gpu_busy_percent", &busy_str)) {
            busy_percent = atof(busy_str.c_str());
        }
        if (read_first_line(base + "/vendor", &vendor_str)) {
            vendor_str = to_lower(vendor_str);
            if (vendor_str == "0x1002") {
                metrics.vendor = "AMD";
            } else if (vendor_str == "0x8086") {
                metrics.vendor = "Intel";
            }
        }

        if (total_mb > 0 || busy_percent >= 0.0) {
            metrics.present = true;
            metrics.total_mb = total_mb;
            metrics.used_mb = used_mb;
            if (total_mb > 0 && used_mb >= 0) {
                metrics.used_percent = static_cast<double>(used_mb) * 100.0 / static_cast<double>(total_mb);
            }
            metrics.core_usage_percent = busy_percent;
            if (metrics.vendor.empty()) {
                metrics.vendor = "DRM GPU";
            }
            closedir(dir);
            return metrics;
        }
    }

    closedir(dir);
    return metrics;
}

GpuMetrics try_read_raspberry_pi_gpu() {
    GpuMetrics metrics;
    if (!command_exists("vcgencmd")) {
        return metrics;
    }

    std::string output;
    if (!run_command("vcgencmd get_mem gpu 2>/dev/null", &output)) {
        return metrics;
    }

    size_t equal_pos = output.find('=');
    if (equal_pos == std::string::npos) {
        return metrics;
    }

    long long total_kb = parse_size_to_kb(output.substr(equal_pos + 1));
    if (total_kb <= 0) {
        return metrics;
    }

    metrics.present = true;
    metrics.vendor = "VideoCore";
    metrics.total_mb = static_cast<long>(total_kb / 1024);
    metrics.note = "Only reserved GPU memory is available on Raspberry Pi.";
    return metrics;
}

GpuMetrics get_gpu_metrics() {
    GpuMetrics metrics = try_read_nvidia_gpu();
    if (metrics.present) {
        return metrics;
    }

    metrics = try_read_drm_gpu();
    if (metrics.present) {
        return metrics;
    }

    return try_read_raspberry_pi_gpu();
}

CmmMetrics get_cmm_metrics() {
    CmmMetrics metrics;
    std::ifstream stream("/proc/ax_proc/mem_cmm_info");
    if (!stream) {
        return metrics;
    }

    std::string line;
    long long total_candidate = -1;
    long long free_candidate = -1;
    long long used_candidate = -1;
    while (std::getline(stream, line)) {
        long long total_kb = extract_any_size_value_kb(
            line, std::vector<std::string>{"total size=", "total_size=", "cmm total size=", "all size="});
        long long free_kb = extract_any_size_value_kb(
            line, std::vector<std::string>{"remain=", "remain size=", "remain_size=", "free=", "free size=", "free_size=", "available=", "available size="});
        long long used_kb = extract_any_size_value_kb(
            line, std::vector<std::string>{"used=", "used size=", "used_size=", "alloc=", "alloc size=", "allocated size=", "use size="});

        if (total_kb >= 0) {
            total_candidate = total_kb;
        }
        if (free_kb >= 0) {
            free_candidate = free_kb;
        }
        if (used_kb >= 0) {
            used_candidate = used_kb;
        }
    }

    if (total_candidate >= 0) {
        metrics.total_kb = static_cast<long>(total_candidate);
    }
    if (free_candidate >= 0) {
        metrics.free_kb = static_cast<long>(free_candidate);
    }
    if (used_candidate >= 0) {
        metrics.used_kb = static_cast<long>(used_candidate);
    }

    if (metrics.used_kb < 0 && metrics.total_kb >= 0 && metrics.free_kb >= 0) {
        metrics.used_kb = metrics.total_kb - metrics.free_kb;
    }
    if (metrics.total_kb > 0 && metrics.used_kb >= 0) {
        metrics.used_percent = static_cast<double>(metrics.used_kb) * 100.0 / static_cast<double>(metrics.total_kb);
    }
    return metrics;
}

std::string get_username() {
    struct passwd *pw = getpwuid(getuid());
    if (pw && pw->pw_name) {
        return pw->pw_name;
    }

    const char *env_user = getenv("USER");
    return env_user ? env_user : "root";
}

std::string detect_libc() {
    std::string output;
    if (run_command("ldd --version 2>&1 | head -n 1", &output)) {
        std::string lower = to_lower(output);
        if (lower.find("musl") != std::string::npos) {
            return "musl";
        }
        if (lower.find("uclibc") != std::string::npos) {
            return "uclibc";
        }
        if (lower.find("glibc") != std::string::npos || lower.find("gnu libc") != std::string::npos) {
            return "glibc";
        }
        return output;
    }

    if (file_exists("/lib/libuClibc-0.9.33.2.so")) {
        return "uclibc";
    }
    return "unknown";
}

std::map<std::string, std::string> parse_os_release() {
    std::map<std::string, std::string> values;
    std::ifstream stream("/etc/os-release");
    if (!stream) {
        return values;
    }

    std::string line;
    while (std::getline(stream, line)) {
        size_t equal = line.find('=');
        if (equal == std::string::npos) {
            continue;
        }
        std::string key = line.substr(0, equal);
        std::string value = line.substr(equal + 1);
        if (!value.empty() && value[0] == '"' && value[value.size() - 1] == '"') {
            value = value.substr(1, value.size() - 2);
        }
        values[key] = value;
    }
    return values;
}

std::string read_board_model() {
    std::string content;
    if (read_file("/proc/device-tree/model", &content)) {
        return strip_after_nul(content);
    }
    if (read_file("/sys/firmware/devicetree/base/model", &content)) {
        return strip_after_nul(content);
    }

    std::string vendor;
    std::string product;
    read_first_line("/sys/class/dmi/id/sys_vendor", &vendor);
    read_first_line("/sys/class/dmi/id/product_name", &product);
    if (!vendor.empty() || !product.empty()) {
        return trim(vendor + " " + product);
    }
    return "";
}

std::string read_board_vendor() {
    std::string vendor;
    if (read_first_line("/sys/class/dmi/id/sys_vendor", &vendor)) {
        return vendor;
    }
    if (read_first_line("/sys/firmware/devicetree/base/serial-number", &vendor)) {
        return vendor;
    }
    return "";
}

std::string normalize_ax_uid(const std::string &uid) {
    size_t pos = uid.find("0x");
    if (pos != std::string::npos) {
        return uid.substr(pos);
    }
    return uid;
}

long read_uptime_seconds() {
    std::ifstream stream("/proc/uptime");
    if (!stream) {
        return -1;
    }
    double uptime = 0.0;
    stream >> uptime;
    return static_cast<long>(uptime);
}

DeviceInfo collect_static_device_info() {
    DeviceInfo info;
    info.user = get_username();
    info.cpu_cores = static_cast<int>(sysconf(_SC_NPROCESSORS_ONLN));
    info.uptime_seconds = read_uptime_seconds();
    info.libc = detect_libc();

    char hostname_buffer[256];
    if (gethostname(hostname_buffer, sizeof(hostname_buffer)) == 0) {
        hostname_buffer[sizeof(hostname_buffer) - 1] = '\0';
        info.hostname = hostname_buffer;
    }

    struct utsname uts;
    if (uname(&uts) == 0) {
        info.machine = uts.machine;
        info.arch = uts.machine;
        info.kernel = uts.release;
    }

    std::map<std::string, std::string> os_release = parse_os_release();
    info.os_pretty_name = os_release["PRETTY_NAME"];
    info.os_name = os_release["NAME"];
    info.os_id = os_release["ID"];
    info.os_like = os_release["ID_LIKE"];
    info.os_version = os_release["VERSION_ID"];

    info.board_model = read_board_model();
    info.board_vendor = read_board_vendor();
    info.is_ax = file_exists("/proc/ax_proc/uid") || file_exists("/proc/ax_proc/board_id");
    info.is_raspberry_pi = to_lower(info.board_model).find("raspberry pi") != std::string::npos;

    if (info.is_ax) {
        info.device_type = "AX";
        info.device_kind = "ax";
        info.platform_vendor = "Axera";
        read_first_line("/proc/ax_proc/uid", &info.uid);
        info.uid = normalize_ax_uid(info.uid);
        read_first_line("/proc/ax_proc/version", &info.version);
        read_first_line("/proc/ax_proc/board_id", &info.board_id);
        if (info.board_model.empty()) {
            info.board_model = info.board_id;
        }
    } else if (info.is_raspberry_pi) {
        info.device_type = "Raspberry Pi";
        info.device_kind = "raspberry_pi";
        info.platform_vendor = "Raspberry Pi";
    } else {
        std::string machine_lower = to_lower(info.machine);
        if (machine_lower.find("x86_64") != std::string::npos || machine_lower.find("amd64") != std::string::npos ||
            machine_lower.find("i686") != std::string::npos || machine_lower.find("i386") != std::string::npos) {
            info.device_type = "x86";
            info.device_kind = "x86";
            info.platform_vendor = info.board_vendor.empty() ? "PC" : info.board_vendor;
        } else if (machine_lower.find("aarch64") != std::string::npos || machine_lower.find("arm") != std::string::npos) {
            info.device_type = "ARM Linux";
            info.device_kind = "arm_linux";
            info.platform_vendor = info.board_vendor.empty() ? "ARM" : info.board_vendor;
        }
    }

    if (info.os_name.empty()) {
        info.os_name = "Linux";
    }
    if (info.os_pretty_name.empty()) {
        info.os_pretty_name = info.os_name;
    }

    return info;
}

std::string format_double(double value) {
    if (value < 0.0) {
        return "null";
    }
    std::ostringstream stream;
    stream << std::fixed << std::setprecision(2) << value;
    return stream.str();
}

std::string format_long(long value) {
    if (value < 0) {
        return "null";
    }
    std::ostringstream stream;
    stream << value;
    return stream.str();
}

std::string format_bool(bool value) {
    return value ? "true" : "false";
}

std::string device_info_to_json(const DeviceInfo &info) {
    std::ostringstream json;
    json << "{";
    json << "\"schema_version\":\"" << json_escape(info.schema_version) << "\",";
    json << "\"hostname\":\"" << json_escape(info.hostname) << "\",";
    json << "\"user\":\"" << json_escape(info.user) << "\",";
    json << "\"device_type\":\"" << json_escape(info.device_type) << "\",";
    json << "\"device_kind\":\"" << json_escape(info.device_kind) << "\",";
    json << "\"platform_vendor\":\"" << json_escape(info.platform_vendor) << "\",";
    json << "\"board_model\":\"" << json_escape(info.board_model) << "\",";
    json << "\"board_vendor\":\"" << json_escape(info.board_vendor) << "\",";
    json << "\"machine\":\"" << json_escape(info.machine) << "\",";
    json << "\"arch\":\"" << json_escape(info.arch) << "\",";
    json << "\"os_pretty_name\":\"" << json_escape(info.os_pretty_name) << "\",";
    json << "\"os_name\":\"" << json_escape(info.os_name) << "\",";
    json << "\"os_id\":\"" << json_escape(info.os_id) << "\",";
    json << "\"os_like\":\"" << json_escape(info.os_like) << "\",";
    json << "\"os_version\":\"" << json_escape(info.os_version) << "\",";
    json << "\"kernel\":\"" << json_escape(info.kernel) << "\",";
    json << "\"libc\":\"" << json_escape(info.libc) << "\",";
    json << "\"uid\":\"" << json_escape(info.uid) << "\",";
    json << "\"version\":\"" << json_escape(info.version) << "\",";
    json << "\"board_id\":\"" << json_escape(info.board_id) << "\",";
    json << "\"is_ax\":" << format_bool(info.is_ax) << ",";
    json << "\"is_raspberry_pi\":" << format_bool(info.is_raspberry_pi) << ",";
    json << "\"cpu_cores\":" << info.cpu_cores << ",";
    json << "\"uptime_seconds\":" << format_long(info.uptime_seconds) << ",";
    json << "\"cpu_usage_percent\":" << format_double(info.cpu_usage_percent) << ",";
    json << "\"mem_total_kb\":" << format_long(info.memory.total_kb) << ",";
    json << "\"mem_available_kb\":" << format_long(info.memory.available_kb) << ",";
    json << "\"mem_free_kb\":" << format_long(info.memory.free_kb) << ",";
    json << "\"mem_used_kb\":" << format_long(info.memory.used_kb) << ",";
    json << "\"mem_buffers_kb\":" << format_long(info.memory.buffers_kb) << ",";
    json << "\"mem_cached_kb\":" << format_long(info.memory.cached_kb) << ",";
    json << "\"mem_sreclaimable_kb\":" << format_long(info.memory.sreclaimable_kb) << ",";
    json << "\"mem_shmem_kb\":" << format_long(info.memory.shmem_kb) << ",";
    json << "\"mem_cache_effective_kb\":" << format_long(info.memory.cache_effective_kb) << ",";
    json << "\"mem_used_percent\":" << format_double(info.memory.used_percent) << ",";
    json << "\"gpu_present\":" << format_bool(info.gpu.present) << ",";
    json << "\"gpu_vendor\":\"" << json_escape(info.gpu.vendor) << "\",";
    json << "\"gpu_mem_total_mb\":" << format_long(info.gpu.total_mb) << ",";
    json << "\"gpu_mem_used_mb\":" << format_long(info.gpu.used_mb) << ",";
    json << "\"gpu_mem_used_percent\":" << format_double(info.gpu.used_percent) << ",";
    json << "\"gpu_usage_percent\":" << format_double(info.gpu.core_usage_percent) << ",";
    json << "\"gpu_note\":\"" << json_escape(info.gpu.note) << "\",";
    json << "\"cmm_total_kb\":" << format_long(info.cmm.total_kb) << ",";
    json << "\"cmm_free_kb\":" << format_long(info.cmm.free_kb) << ",";
    json << "\"cmm_used_kb\":" << format_long(info.cmm.used_kb) << ",";
    json << "\"cmm_used_percent\":" << format_double(info.cmm.used_percent) << ",";
    json << "\"timestamp_ms\":" << info.timestamp_ms;
    json << "}";
    return json.str();
}

}  // namespace

int main() {
    const int broadcast_port = get_env_int("BROADCAST_PORT", kDefaultBroadcastPort);
    const std::string broadcast_ip = get_env_or_default("BROADCAST_IP", kDefaultBroadcastIp);
    const int interval_seconds = get_env_int("BROADCAST_INTERVAL_SECONDS", kDefaultIntervalSeconds);

    int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        std::perror("socket");
        return 1;
    }

    int broadcast_enable = 1;
    if (setsockopt(sockfd, SOL_SOCKET, SO_BROADCAST, &broadcast_enable, sizeof(broadcast_enable)) < 0) {
        std::perror("setsockopt(SO_BROADCAST)");
        close(sockfd);
        return 1;
    }

    sockaddr_in broadcast_addr;
    memset(&broadcast_addr, 0, sizeof(broadcast_addr));
    broadcast_addr.sin_family = AF_INET;
    broadcast_addr.sin_port = htons(static_cast<uint16_t>(broadcast_port));
    broadcast_addr.sin_addr.s_addr = inet_addr(broadcast_ip.c_str());

    DeviceInfo base_info = collect_static_device_info();
    CpuSample cpu_sample;

    std::cout << "device_broadcast started: " << base_info.device_type
              << " " << base_info.hostname << " -> " << broadcast_ip
              << ":" << broadcast_port << std::endl;

    while (true) {
        DeviceInfo info = base_info;
        info.cpu_usage_percent = get_cpu_usage(&cpu_sample);
        info.memory = get_memory_metrics();
        info.gpu = get_gpu_metrics();
        if (info.is_ax) {
            info.cmm = get_cmm_metrics();
        }
        info.uptime_seconds = read_uptime_seconds();
        info.timestamp_ms = static_cast<long long>(time(NULL)) * 1000LL;

        std::string payload = device_info_to_json(info);
        if (payload.size() >= kBufferSize) {
            std::cerr << "payload too large: " << payload.size() << std::endl;
        } else if (sendto(sockfd, payload.c_str(), payload.size(), 0,
                          reinterpret_cast<sockaddr *>(&broadcast_addr),
                          sizeof(broadcast_addr)) < 0) {
            std::perror("sendto");
        }

        sleep(interval_seconds);
    }

    close(sockfd);
    return 0;
}
